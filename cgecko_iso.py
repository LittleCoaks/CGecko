#!/usr/bin/env python3
"""cgecko_iso.py -- build a C file into a DOL-BAKED blob instead of gecko codes.

    python cgecko_iso.py <file.c> --load-addr 0x80001810 --out payload.bin

Emits `payload.bin` (loaded verbatim at --load-addr, e.g. as a new DOL text
section) and `payload.json`, a manifest naming every hook's wrapper address so
the caller can write a `bl` to it (C2) or call it once per frame (C0).

WHY THIS IS NOT THE GECKO PATH
    A gecko payload lands wherever Project Rio puts the code list at runtime, so
    cgecko compiles position-independent: .picdata is linked at VMA 0, every
    `lis rN, 0` that forms a data address is rewritten to `mr rN, r31`, and
    pointers the linker baked into data get a runtime fixup loop.

    A DOL section is loaded at exactly the address in the DOL header and nothing
    relocates it (verified: a marker section at 0x80002000 reads back
    byte-identical after boot). So the linker can resolve absolute addresses
    normally and that entire PIC layer is unnecessary here -- no lis patching,
    no .picdata reloc table, no r31 base. Smaller output, and the whole class of
    data-pointer relocation bugs simply does not arise.

    Second difference: ONE link for the file, not one per hook. The gecko path
    links each hook separately with --gc-sections, so a helper used by two hooks
    (the ScreenText formatter, say) is duplicated into both payloads. Here every
    hook body is a root in a single link and shared code exists once.

WHAT IS KEPT
    The BACKUP/RESTORE wrapper, unchanged -- we are still interrupting the game
    mid-function, so the same registers must be saved. Each wrapper is

        BACKUP                  ; mflr r0; stw r0,4(r1); stwu r1,-frame(r1);
                                ;   stmw r3,8(r1); mr r30,r1; [stfd fN...]
        bl   <hook body>
        RESTORE
        [displaced instruction] ; the .instruction the hook site overwrote
        blr                     ; back to just after the `bl` we replaced

    r30 stays reserved (-ffixed-r30) because BACKUP uses it as the frame base
    for READ_GAME_REG/WRITE_GAME_REG.
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


def make_linker_script(load_addr):
    """Everything at its real address, one section per kind, .bss last."""
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
    """name -> value, from readelf -sW."""
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
    """Highest address covered by .text/.rodata/.data/.bss."""
    out = subprocess.run([cg.READELF, "-SW", elf_path],
                         capture_output=True, text=True).stdout
    end = load_addr
    for line in out.splitlines():
        m = re.search(r"\s\.(text|rodata|data|bss)\s+\w+\s+([0-9a-f]+)\s+[0-9a-f]+\s+([0-9a-f]+)",
                      line)
        if m:
            end = max(end, int(m.group(2), 16) + int(m.group(3), 16))
    return end


def build(c_path, load_addr, debug=False):
    """Returns (blob_bytes, manifest_list)."""
    tmpdir = tempfile.mkdtemp(prefix="cgecko_iso_")
    obj = os.path.join(tmpdir, "unit.o")
    cg.compile_c(c_path, obj, debug)

    hooks = cg.read_hook_records(obj)
    if not hooks:
        cg.die("No CGECKO / ASM records found in " + c_path)

    stripped = os.path.join(tmpdir, "unit.stripped.o")
    cg.strip_section(obj, stripped, ".cgecko_hooks")

    ld = os.path.join(tmpdir, "iso.ld")
    with open(ld, "w") as f:
        f.write(make_linker_script(load_addr))
    elf = os.path.join(tmpdir, "iso.elf")
    # No --gc-sections and no --entry: every hook body is a root.
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
        img = img + bytes(total - len(img))       # zero-fill .bss

    syms = elf_symbols(elf)
    used_fprs = cg.detect_used_fprs(img, set(), debug)
    frame = cg.compute_frame_size(used_fprs)
    backup = cg.build_backup(frame, used_fprs)
    restore = cg.build_restore(frame, used_fprs)

    blob, manifest = bytearray(img), []
    for h in hooks:
        body = syms.get(h["entry"])
        if body is None:
            cg.die("Hook '%s' is not in the linked image." % h["entry"])
        wrapper = load_addr + len(blob)
        w = bytearray(backup)
        bl_from = wrapper + len(w)
        w += struct.pack(">I", cg.BL_BASE | ((body - bl_from) & 0x03FFFFFC))
        w += restore
        if h["instruction"]:
            w += struct.pack(">I", cg.assemble_instruction(h["instruction"], tmpdir))
        if h["address"]:
            # C2: branch back to site+4. The SITE gets `b wrapper`, NOT `bl` --
            # a `bl` would overwrite LR with site+4, and a REPLACE hook
            # (.instruction = "blr", e.g. Options Menu / Custom Menu Scene /
            # Teams Exhibition 2) would then "return" into the middle of the
            # function it is supposed to be replacing instead of to the
            # function's caller. Using `b` leaves the game's LR untouched, so
            # the displaced blr returns where the game expects and this trailing
            # branch is simply unreachable. Augment hooks fall through to it
            # normally. Same shape cgecko's own C2 emits.
            back = wrapper + len(w)
            w += struct.pack(">I", cg.B_BASE | ((h["address"] + 4 - back) & 0x03FFFFFC))
        else:
            w += struct.pack(">I", cg.BLR_INSTR)   # C0: called with bl, return
        blob += w
        manifest.append({
            "name": h["entry"],
            "kind": "c0" if not h["address"] else "c2",
            "site": h["address"],
            "site_branch": "b" if h["address"] else None,
            "body": body,
            "wrapper": wrapper,
            "state": h["state"],
            "gate_addr": h["gate_addr"],
            "gate_value": h["gate_value"],
            "instruction": h["instruction"],
        })
    return bytes(blob), manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="C source (a mod pack)")
    ap.add_argument("--load-addr", required=True,
                    help="Address the blob is loaded at, e.g. 0x80001810")
    ap.add_argument("--out", required=True, help="Output .bin path")
    ap.add_argument("-d", action="store_true", help="verbose")
    a = ap.parse_args()

    load = int(a.load_addr, 0)
    blob, manifest = build(os.path.abspath(a.input), load, a.d)
    with open(a.out, "wb") as f:
        f.write(blob)
    with open(os.path.splitext(a.out)[0] + ".json", "w") as f:
        json.dump({"load_addr": load, "size": len(blob), "hooks": manifest}, f, indent=2)

    print("[INFO] blob      : %s  (%d bytes at 0x%08X-0x%08X)"
          % (a.out, len(blob), load, load + len(blob)))
    for h in manifest:
        site = ("site 0x%08X" % h["site"]) if h["site"] else "per-frame"
        gate = ("  gate [0x%08X]==%d" % (h["gate_addr"], h["gate_value"])) if h["gate_addr"] else ""
        print("       %-24s %-3s %-16s wrapper 0x%08X%s"
              % (h["name"], h["kind"], site, h["wrapper"], gate))


if __name__ == "__main__":
    main()
