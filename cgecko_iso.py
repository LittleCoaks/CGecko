#!/usr/bin/env python3
"""cgecko_iso.py -- build a C file into a DOL-BAKED blob instead of gecko codes.

    python cgecko_iso.py pack.c --load-addr 0x80001810 --out payload.bin \\
                         --frame-site 0x80009404 --frame-instr "mr r3, r25"

Emits `payload.bin` (loaded verbatim at --load-addr as a new DOL text section)
and `payload.json`, a manifest naming every branch the caller must write into
the DOL.

WHY THIS IS NOT THE GECKO PATH
    A gecko payload lands wherever Project Rio puts the code list at runtime, so
    cgecko compiles position-independent: .picdata at VMA 0, every `lis rN, 0`
    that forms a data address rewritten to `mr rN, r31`, plus a runtime fixup
    loop for pointers the linker baked into data.

    A DOL section is loaded at exactly the address in the DOL header and nothing
    relocates it (verified: a marker section at 0x80002000 reads back
    byte-identical after boot). The linker can therefore resolve absolute
    addresses normally and that whole PIC layer is dropped -- smaller output,
    and its class of data-pointer relocation bugs cannot arise.

    Second difference: ONE link for the file, not one per hook, so a helper two
    hooks share exists once instead of being duplicated into both payloads.

TWO PASSES, BECAUSE GATES BECOME C
    In gecko mode a hook's `.state` and `CGECKO_GATE_ADDR` become gecko
    conditionals (`28...` / `20...` / `E2000001`) wrapped around the code. There
    is no interpreter here, so those checks are emitted as ORDINARY C instead:

      pass 1  compile the pack, read its .cgecko_hooks records
      pass 2  generate a source that #includes the pack and adds
                - one shim per gated hook   : if (state && gate) body();
                - one frame dispatcher      : calls every C0 in order
              then compile and link THAT as the single translation unit

    Nothing about the pack or the stock mods changes; the generated file is
    build-time scaffolding.

WHAT IS KEPT
    The BACKUP/RESTORE wrapper, unchanged -- we are still interrupting the game
    mid-function. Each wrapper is

        BACKUP                  ; mflr r0; stw r0,4(r1); stwu r1,-frame(r1);
                                ;   stmw r3,8(r1); mr r30,r1; [stfd fN...]
        bl   <body or shim>
        RESTORE
        [displaced instruction] ; whatever the hook site overwrote
        b    site+4

    The SITE gets `b wrapper`, never `bl`: a `bl` would overwrite LR with
    site+4, and a REPLACE hook (.instruction = "blr") would then return into the
    middle of the function it is meant to replace instead of to that function's
    caller. With `b` the game's LR is untouched.
"""
import argparse
import json
import os
import re
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cgecko as cg

FRAME_FN = "_cgecko_iso_frame"
REL_STATE_ADDR = 0x800E877C          # halfword: 0 boot, 4 menus.rel, 5 game.rel
STATE_VALUE = {1: 0, 2: 4, 3: 5}     # MSSB_BOOT / MSSB_MENU / MSSB_GAME
REL_BASE = 0x8063F094                # menus.rel / game.rel share this arena slot


def rel_branch(frm, to):
    """`b` with a range check -- silently truncating one would be a wild jump."""
    d = to - frm
    if not (-0x2000000 <= d < 0x2000000):
        cg.die("branch 0x%08X -> 0x%08X is %d bytes, out of the +/-32 MB range."
               % (frm, to, d))
    return cg.B_BASE | (d & 0x03FFFFFC)


def make_linker_script(load_addr):
    return (
        "SECTIONS {" "\n"
        "    . = 0x%08X;" "\n"
        "    .text : { *(.text*) }" "\n"
        "    . = ALIGN(4);" "\n"
        "    .rodata : { *(.rodata*) }" "\n"
        "    . = ALIGN(4);" "\n"
        "    .data : { *(.data*) *(.sdata*) *(.sdata2*) }" "\n"
        "    . = ALIGN(4);" "\n"
        "    .bss : { *(.sbss*) *(.bss*) *(COMMON) }" "\n"
        "    /DISCARD/ : {" "\n"
        "        *(.cgecko_hooks) *(.comment) *(.gnu.attributes)" "\n"
        "        *(.eh_frame*) *(.pdr)" "\n"
        "    }" "\n"
        "}" "\n"
    ) % load_addr


def elf_symbols(elf_path):
    out = subprocess.run([cg.READELF, "-sW", elf_path],
                         capture_output=True, text=True).stdout
    syms = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 8 and p[0].endswith(":"):
            try:
                syms[p[7]] = int(p[1], 16)
            except ValueError:
                pass
    return syms


def image_end(elf_path, load_addr):
    out = subprocess.run([cg.READELF, "-SW", elf_path],
                         capture_output=True, text=True).stdout
    end = load_addr
    for line in out.splitlines():
        m = re.search(r"\s\.(text|rodata|data|bss)\s+\w+\s+([0-9a-f]+)\s+[0-9a-f]+\s+([0-9a-f]+)",
                      line)
        if m:
            end = max(end, int(m.group(2), 16) + int(m.group(3), 16))
    return end


def _cond(hook):
    """The C condition guarding one hook, or None when it always runs."""
    parts = []
    st = STATE_VALUE.get(hook["state"])
    if st is not None:
        parts.append("*(volatile unsigned short*)0x%08XU == %d" % (REL_STATE_ADDR, st))
    if hook["gate_addr"]:
        parts.append("*(volatile unsigned long*)0x%08XU == 0x%XU"
                     % (hook["gate_addr"], hook["gate_value"]))
    return " && ".join(parts) if parts else None


PATCH_TABLE = "_iso_rel_patches"
DC_FLUSH = 0x8006E894
IC_INVALIDATE = 0x8006E94C


def generate_source(pack_path, hooks):
    """Pass-2 scaffolding: the pack, plus shims for gated hooks and the frame."""
    inc = os.path.abspath(pack_path).replace("\\", "/")
    rel_hooks = [h for h in hooks if h["address"] and h["address"] >= REL_BASE]
    out = ['/* GENERATED by cgecko_iso.py -- do not edit. */',
           '#include "%s"' % inc, ""]

    if rel_hooks:
        # Triples (rel, site, branch word), filled in by the builder AFTER the
        # wrappers are laid out -- their addresses are not known until then, so
        # the table cannot be a compile-time initialiser.
        out += [
            "/* REL re-applier. A branch written into REL space is wiped the next",
            " * time a REL loads, so it is re-checked every frame and rewritten",
            " * when missing. The compare makes the common case a load, not a",
            " * store + cache flush. Guarded on the resident REL: writing a",
            " * menus.rel site while game.rel is loaded would corrupt game code,",
            " * since the two share one arena slot. */",
            "#define _ISO_RELP %d" % len(rel_hooks),
            "unsigned long %s[_ISO_RELP * 3];" % PATCH_TABLE,
            "",
            "static void _iso_reapply(void)",
            "{",
            "    unsigned short rel = *(volatile unsigned short*)0x%08XU;" % REL_STATE_ADDR,
            "    int i;",
            "    for (i = 0; i < _ISO_RELP; i++) {",
            "        unsigned long site = %s[i * 3 + 1];" % PATCH_TABLE,
            "        unsigned long word = %s[i * 3 + 2];" % PATCH_TABLE,
            "        if (!site) continue;",
            "        if (rel != %s[i * 3]) continue;" % PATCH_TABLE,
            "        if (*(volatile unsigned long*)site == word) continue;",
            "        *(volatile unsigned long*)site = word;",
            "        ((void (*)(unsigned long, unsigned long))0x%08XU)(site & ~31UL, 64);" % DC_FLUSH,
            "        ((void (*)(unsigned long, unsigned long))0x%08XU)(site & ~31UL, 64);" % IC_INVALIDATE,
            "    }",
            "}",
            "",
        ]
    shims = {}
    for h in hooks:
        if not h["address"]:
            continue                                  # C0s go in the dispatcher
        c = _cond(h)
        if not c:
            continue                                  # ungated: wrap the body
        name = "_iso_shim_" + h["entry"]
        shims[h["entry"]] = name
        out.append("void %s(void) { if (%s) %s(); }" % (name, c, h["entry"]))
    out.append("")
    out.append("void %s(void)" % FRAME_FN)
    out.append("{")
    if rel_hooks:
        out.append("    _iso_reapply();")
    n = 0
    for h in hooks:
        if h["address"]:
            continue
        c = _cond(h)
        out.append("    if (%s) %s();" % (c, h["entry"]) if c
                   else "    %s();" % h["entry"])
        n += 1
    if not n and not rel_hooks:
        out.append("    /* no per-frame hooks */")
    out.append("}")
    return "\n".join(out) + "\n", shims


def build(pack_path, load_addr, frame_site=None, frame_instr=None, debug=False):
    tmpdir = tempfile.mkdtemp(prefix="cgecko_iso_")

    # ---- pass 1: what hooks does the pack declare? ------------------------
    obj1 = os.path.join(tmpdir, "pass1.o")
    cg.compile_c(pack_path, obj1, debug)
    hooks = cg.read_hook_records(obj1)
    if not hooks:
        cg.die("No CGECKO / ASM records found in " + pack_path)
    for h in hooks:
        if h["address"] and h["address"] >= REL_BASE and h["state"] not in (2, 3):
            cg.die("Hook '%s' targets REL space (0x%08X) but its .state is not "
                   "MSSB_MENU or MSSB_GAME. menus.rel and game.rel share one arena "
                   "slot, so the re-applier must know which REL the site belongs to "
                   "-- writing it under the wrong one corrupts the other module."
                   % (h["entry"], h["address"]))
        if h["address"] and h["gate_addr"] and (h["instruction"] or "").strip() == "blr":
            cg.warn("Hook '%s' is a REPLACE hook (.instruction = \"blr\") AND gated. "
                    "The site branch is static, so when the gate is off the body is "
                    "skipped but the blr still fires and the function is still "
                    "replaced. Gate a REPLACE hook at the source instead."
                    % h["entry"])

    # ---- pass 2: generate scaffolding, compile the whole thing ------------
    gen_src, shims = generate_source(pack_path, hooks)
    gen_path = os.path.join(tmpdir, "_iso_gen.c")
    with open(gen_path, "w", encoding="utf-8") as f:
        f.write(gen_src)
    if debug:
        print("[DEBUG] generated scaffolding:\n" + gen_src)

    obj = os.path.join(tmpdir, "unit.o")
    cg.compile_c(gen_path, obj, debug)
    stripped = os.path.join(tmpdir, "unit.stripped.o")
    cg.strip_section(obj, stripped, ".cgecko_hooks")

    ld = os.path.join(tmpdir, "iso.ld")
    with open(ld, "w") as f:
        f.write(make_linker_script(load_addr))
    elf = os.path.join(tmpdir, "iso.elf")
    r = subprocess.run([cg.LD, "-T", ld, stripped, cg.get_libgcc(), "-o", elf],
                       capture_output=True, text=True)
    if r.returncode != 0:
        cg.die("Link failed (iso blob):\n" + r.stderr)

    binp = os.path.join(tmpdir, "img.bin")
    subprocess.run([cg.OBJCOPY, "-O", "binary",
                    "--only-section=.text", "--only-section=.rodata",
                    "--only-section=.data", elf, binp], capture_output=True)
    img = open(binp, "rb").read() if os.path.isfile(binp) else b""
    total = image_end(elf, load_addr) - load_addr
    if len(img) < total:
        img = img + bytes(total - len(img))          # zero-fill .bss

    syms = elf_symbols(elf)
    used_fprs = cg.detect_used_fprs(img, set(), debug)
    frame = cg.compute_frame_size(used_fprs)
    backup = cg.build_backup(frame, used_fprs)
    restore = cg.build_restore(frame, used_fprs)
    blob = bytearray(img)

    def wrap(target_name, site, instr_text):
        """BACKUP; bl target; RESTORE; [displaced]; b site+4  -> wrapper addr."""
        body = syms.get(target_name)
        if body is None:
            cg.die("'%s' is not in the linked image." % target_name)
        addr = load_addr + len(blob)
        w = bytearray(backup)
        w += struct.pack(">I", cg.BL_BASE | ((body - (addr + len(w))) & 0x03FFFFFC))
        w += restore
        if instr_text:
            w += struct.pack(">I", cg.assemble_instruction(instr_text, tmpdir))
        w += struct.pack(">I", rel_branch(addr + len(w), site + 4))
        blob.extend(w)
        return addr

    manifest = []
    for h in hooks:
        if not h["address"]:
            continue                                  # dispatched per frame
        target = shims.get(h["entry"], h["entry"])
        manifest.append({
            "name": h["entry"], "kind": "c2", "site": h["address"],
            "site_branch": "b",
            "wrapper": wrap(target, h["address"], h["instruction"]),
            "gated_via": shims.get(h["entry"]),
            "state": h["state"], "gate_addr": h["gate_addr"],
            "gate_value": h["gate_value"], "instruction": h["instruction"],
            "rel": h["address"] >= REL_BASE,
        })

    # Fill the REL patch table now that every wrapper has an address. It lives in
    # the blob's .bss, which is emitted zero-filled above, so writing it here
    # means the values are already in place when the section loads.
    rel_hooks = [h for h in manifest if h["rel"]]
    if rel_hooks:
        tbl = syms.get(PATCH_TABLE)
        if tbl is None:
            cg.die("%s missing from the linked image." % PATCH_TABLE)
        off = tbl - load_addr
        if off < 0 or off + len(rel_hooks) * 12 > len(blob):
            cg.die("%s at 0x%08X falls outside the blob." % (PATCH_TABLE, tbl))
        for i, h in enumerate(rel_hooks):
            word = rel_branch(h["site"], h["wrapper"])
            struct.pack_into(">III", blob, off + i * 12,
                             STATE_VALUE[h["state"]], h["site"], word)
            h["branch_word"] = word
        print("[INFO] REL re-applier: %d site(s), table at 0x%08X"
              % (len(rel_hooks), tbl))

    frame_entry = None
    if frame_site is not None:
        frame_entry = {
            "name": FRAME_FN, "kind": "frame", "site": frame_site,
            "site_branch": "b",
            "wrapper": wrap(FRAME_FN, frame_site, frame_instr),
            "instruction": frame_instr,
            "calls": [h["entry"] for h in hooks if not h["address"]],
        }
    return bytes(blob), manifest, frame_entry


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="C source (a mod pack)")
    ap.add_argument("--load-addr", required=True, help="e.g. 0x80001810")
    ap.add_argument("--out", required=True, help="output .bin")
    ap.add_argument("--frame-site", help="DOL address to call the per-frame entry from")
    ap.add_argument("--frame-instr", help="instruction that site overwrites, e.g. 'mr r3, r25'")
    ap.add_argument("-d", action="store_true")
    a = ap.parse_args()

    load = int(a.load_addr, 0)
    site = int(a.frame_site, 0) if a.frame_site else None
    blob, manifest, frame = build(os.path.abspath(a.input), load, site, a.frame_instr, a.d)

    with open(a.out, "wb") as f:
        f.write(blob)
    with open(os.path.splitext(a.out)[0] + ".json", "w") as f:
        json.dump({"load_addr": load, "size": len(blob),
                   "frame": frame, "hooks": manifest}, f, indent=2)

    print("[INFO] blob      : %d bytes at 0x%08X-0x%08X" % (len(blob), load, load + len(blob)))
    if frame:
        print("[INFO] per-frame : b 0x%08X at site 0x%08X   calls: %s"
              % (frame["wrapper"], frame["site"], ", ".join(frame["calls"]) or "(none)"))
    for h in manifest:
        note = "  REL (needs runtime re-apply)" if h["rel"] else "  DOL (static branch)"
        gate = "  gated via %s" % h["gated_via"] if h["gated_via"] else ""
        print("[INFO] hook      : %-22s b 0x%08X at 0x%08X%s%s"
              % (h["name"], h["wrapper"], h["site"], note, gate))


if __name__ == "__main__":
    main()
