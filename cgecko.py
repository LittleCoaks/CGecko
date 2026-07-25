#!/usr/bin/env python3
"""cgecko.py — Converts a .c or .asm file into a C2/C3 Gecko code for GameCube modding.
See README.md for full documentation."""
import sys
import os
import re
import struct
import subprocess
import tempfile
import shutil
import argparse
import json
from typing import NoReturn
# ==============================================================================
# CONFIGURATION
# ==============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEVKITPPC_BIN = os.path.normpath(os.path.join(
    os.environ.get("DEVKITPPC", os.path.join("C:\\devkitpro", "devkitPPC")),
    "bin"
))
def tool(name: str) -> str:
    exe = name + ".exe" if sys.platform == "win32" else name
    return os.path.join(DEVKITPPC_BIN, exe)
GCC     = tool("powerpc-eabi-gcc")
AS      = tool("powerpc-eabi-as")
LD      = tool("powerpc-eabi-ld")
OBJCOPY = tool("powerpc-eabi-objcopy")
OBJDUMP = tool("powerpc-eabi-objdump")
READELF = tool("powerpc-eabi-readelf")
GCC_FLAGS = [
    "-DGEKKO",
    "-mogc",
    "-mcpu=750",
    "-meabi",
    "-mno-sdata",    # all static data goes to .rodata/.data, accessed via lis/ori (PIC-patchable)
    "-fno-jump-tables",  # switch/if-chain jump tables embed link-time code addresses,
                         # which are wrong at runtime (payload layout differs by the
                         # 4-byte mflr r31 between .picdata and .text, and code is
                         # never relocated) -- force compare chains instead
    "-mhard-float",
    "-fomit-frame-pointer",
    "-ffunction-sections",
    "-fno-asynchronous-unwind-tables",
    "-fno-optimize-sibling-calls",
    "-O1",
    "-Wno-attributes",
    f"-I{os.path.dirname(SCRIPT_DIR)}",
    f"-I{SCRIPT_DIR}",   # Common.h ships next to cgecko.py
    "-include", "Common.h",  # hook macros/helpers are always in scope; an explicit
                             # #include <Common.h> in the mod is still fine (guarded)
    "-c",
    "-ffixed-r30",   # r30 = saved frame base for macro stack access
    "-ffixed-r31",   # r31 = PIC register (data table base address)
]
AS_FLAGS = [
    "-mregnames",
    "-mbig",
    f"-I{os.path.dirname(SCRIPT_DIR)}",
]
# ==============================================================================
# CONSTANTS
# ==============================================================================
BLR_INSTR  = 0x4E800020
NOP_INSTR  = 0x60000000
TERMINATOR = 0x00000000
B_BASE     = 0x48000000
GPR_SAVE_OFFSET = 0x8
FPR_BASE_OFFSET = 0x88
FPR_SLOT_SIZE   = 8
FRAME_MIN       = 0x90
MFLR_R0  = (31 << 26) | (0  << 21) | (8 << 16) | (339 << 1)
MTLR_R0  = (31 << 26) | (0  << 21) | (8 << 16) | (467 << 1)
MFLR_R31 = (31 << 26) | (31 << 21) | (8 << 16) | (339 << 1)  # mflr r31
BL_BASE  = 0x48000001  # bl (PC-relative, link bit set)
# ==============================================================================
# RAW GECKO INI PARSING
# ==============================================================================
_HEX_PAIR = re.compile(r'^[0-9A-Fa-f]{8} [0-9A-Fa-f]{8}$')
def parse_gecko_blocks(source: str) -> list[tuple[str, str]]:
    """Parse raw gecko code blocks from a .ini source file.
    Each block starts with a $Name header line. Hex lines are validated;
    unrecognized lines trigger a warning and are skipped.
    Returns a list of (name, gecko_code_string) pairs."""
    blocks: list[tuple[str, str]] = []
    header: str | None = None
    lines:  list[str]  = []
    def _flush():
        if header is None:
            return
        if not any(_HEX_PAIR.match(l) for l in lines if not l.startswith("*")):
            warn(f"Block '{header}' has no valid hex lines — skipping.")
            return
        h     = header.lstrip("$")
        bracket = h.find("[")
        name  = (h[:bracket] if bracket != -1 else h).strip()
        blocks.append((name, "\n".join([header] + lines)))
    for lineno, raw in enumerate(source.splitlines(), 1):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("$"):
            _flush()
            header = stripped
            lines  = []
        elif header is None:
            warn(f"Line {lineno}: data before any $header — skipped.")
        elif stripped.startswith("*") or _HEX_PAIR.match(stripped):
            lines.append(stripped)
        else:
            warn(f"Line {lineno}: unrecognized '{stripped}' — skipped.")
    _flush()
    return blocks
# ==============================================================================
# COMMENT PARSING
# ==============================================================================
def _pat(key: str, value_re: str, flags=re.IGNORECASE | re.MULTILINE) -> re.Pattern:
    return re.compile(rf"(?://|#)\s*{key}\s*:\s*{value_re}", flags)
ADDRESS_PATTERN     = re.compile(
    r"(?://|#)\s*(?:Address|Inject|Entry)\s*:\s*(0x[0-9A-Fa-f]{8})",
    re.IGNORECASE | re.MULTILINE
)
AUTHOR_PATTERN      = _pat("Author",      r"(.+?)(?:\n|$)")
INSTRUCTION_PATTERN = _pat("Instruction", r"(.+?)(?:\n|$)")
STATE_PATTERN       = _pat("State",       r"(.+?)(?:\n|$)")
def parse_author(source: str) -> str | None:
    m = AUTHOR_PATTERN.search(source)
    return m.group(1).strip() if m else None
# State values map to Project Rio scene IDs
STATE_MAP = {
    "boot": (0x280E877C, 0x00000000),
    "0":    (0x280E877C, 0x00000000),
    "menu": (0x280E877C, 0x00000004),
    "4":    (0x280E877C, 0x00000004),
    "game": (0x280E877C, 0x00000005),
    "5":    (0x280E877C, 0x00000005),
}
def parse_state(source: str) -> tuple[int, int] | None:
    m = STATE_PATTERN.search(source)
    if not m:
        return None
    key = m.group(1).strip().lower()
    if key not in STATE_MAP:
        die(f"Unknown State value '{key}'. Expected: boot, menu, game, 0, 4, or 5.")
    return STATE_MAP[key]
def parse_file_state(source: str) -> tuple[int, int] | None:
    """File-level State: — read ONLY from the preamble (everything before the
    first // Address:). A State: inside a section belongs to that section, and
    must not be picked up here as though it applied to the whole file."""
    m = ADDRESS_PATTERN.search(source)
    head = source[:m.start()] if m else source
    return parse_state(head)
def parse_notes(source: str, is_asm: bool = False) -> list[str]:
    # A note is the file's comment marker directly followed by '*': '// *' in C,
    # '# *' in ASM. Keying off the language-specific marker stops a C block
    # comment's close ('###*/' — a '#' right before '*') being read as a note.
    marker  = r"#" if is_asm else r"//"
    pattern = re.compile(rf"(?:{marker})\s*(\*[^\n]*)", re.MULTILINE)
    return [m.group(1).strip() for m in pattern.finditer(source)]
# ==============================================================================
# INSTRUCTION ASSEMBLY
# ==============================================================================
def assemble_instruction(asm_text: str, tmpdir: str) -> int:
    """Assemble a single PPC instruction string to its 4-byte integer encoding."""
    asm_src = os.path.join(tmpdir, "instr.s")
    asm_obj = os.path.join(tmpdir, "instr.o")
    asm_bin = os.path.join(tmpdir, "instr.bin")
    with open(asm_src, "w") as f:
        f.write(f".text\n{asm_text}\n")
    result = subprocess.run(
        [AS] + AS_FLAGS + [asm_src, "-o", asm_obj],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        die(f"Failed to assemble instruction '{asm_text}':\n{result.stderr}")
    result = subprocess.run(
        [OBJCOPY, "-O", "binary", "--only-section", ".text", asm_obj, asm_bin],
        capture_output=True
    )
    if result.returncode != 0 or not os.path.isfile(asm_bin):
        die(f"Failed to extract bytes for '{asm_text}'.")
    with open(asm_bin, "rb") as f:
        data = f.read()
    if len(data) < 4:
        die(f"Instruction '{asm_text}' assembled to {len(data)} bytes (expected 4).")
    return struct.unpack(">I", data[:4])[0]
# ==============================================================================
# ASM SECTION BUILDER
# ==============================================================================
def _build_asm_section(section: dict, idx: int, tmpdir: str, debug: bool) -> list[str]:
    """Assemble one # Address: section of an ASM file into gecko code lines.
    The user's ASM is used VERBATIM — no blr rewriting. A C2 runs inline and
    falls through to the handler's appended branch-back, so author your ASM to
    end by falling through (do not put a trailing blr unless you mean to return
    through the game's link register)."""
    addr     = section["address"]
    src_text = section["source"]
    tag      = f"sec{idx}_{addr:08x}"
    src_path = os.path.join(tmpdir, f"{tag}.s")
    obj_path = os.path.join(tmpdir, f"{tag}.o")
    with open(src_path, "w") as f:
        f.write(src_text)
    assemble_asm(src_path, obj_path, debug)
    payload = extract_section(obj_path, ".text")
    if not payload:
        die(f"Section {idx + 1} at {addr:#010x} produced no .text output.")
    if len(payload) == 4:
        instr_word = struct.unpack(">I", payload)[0]
        lines = format_04(addr, instr_word)
        print(f"[INFO]   {addr:#010x} → 04 write")
    else:
        payload = pad_and_terminate(payload, None, debug)
        lines   = format_c2(addr, payload)
        print(f"[INFO]   {addr:#010x} → C2  ({len(payload) // 4} words)")
    return lines
CONFIG_PATH = os.path.join(os.path.dirname(SCRIPT_DIR), "config.json")
TXT_PATH    = os.path.join(os.path.dirname(SCRIPT_DIR), "codes.txt")
def load_config() -> dict:
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return {}
def get_ini_path()     -> str | None: return load_config().get("ini_path")
def get_build_file()   -> str | None: return load_config().get("build_file")
def get_dolphin_path() -> str | None: return load_config().get("dolphin_path")
def get_iso_path()     -> str | None: return load_config().get("iso_path")
def get_launch()       -> bool:       return bool(load_config().get("launch dolphin", False))
def find_latest_source() -> str | None:
    """Find the most recently modified .c/.asm/.ini file under the project root
    (the parent of the CGecko folder). Returns its path, or None if none found.
    Hidden directories (.git, .vscode, ...) are skipped."""
    root = os.path.dirname(SCRIPT_DIR)
    exts = (".c", ".asm", ".ini")
    newest, newest_mtime = None, -1.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.lower().endswith(exts):
                full = os.path.join(dirpath, fn)
                try:
                    mt = os.path.getmtime(full)
                except OSError:
                    continue
                if mt > newest_mtime:
                    newest, newest_mtime = full, mt
    return newest
# ==============================================================================
# GECKO OUTPUT FORMATTING
# ==============================================================================
def build_gecko_output(code_lines: list[str],
                       name:        str,
                       author:      str | None,
                       notes:       list[str],
                       cond_value:  int | None,
                       cond_addr:   int | None) -> str:
    out_lines = []
    header = f"${name}"
    if author is not None:
        header += f" [{author}]"
    out_lines.append(header)
    if cond_value is not None and cond_addr is not None:
        out_lines.append(f"{cond_addr:08X} {cond_value:08X}")
    out_lines.extend(code_lines)
    if cond_value is not None:
        out_lines.append("E2000001 00000000")
    for note in notes:
        out_lines.append(note)
    return "\n".join(out_lines)
def format_c2(inject_addr: int, payload: bytes) -> list[str]:
    assert len(payload) % 8 == 0
    code_type = 0xC3 if inject_addr >= 0x81000000 else 0xC2
    if code_type == 0xC3:
        print("[INFO] Address >= 0x81000000 — using C3 code type.")
    header = (code_type << 24) | (inject_addr & 0x00FFFFFF)
    lines  = [f"{header:08X} {len(payload)//8:08X}"]
    for i in range(0, len(payload), 8):
        w1, w2 = struct.unpack(">II", payload[i:i+8])
        lines.append(f"{w1:08X} {w2:08X}")
    return lines
def format_04(inject_addr: int, instr: int) -> list[str]:
    header = (0x04 << 24) | (inject_addr & 0x00FFFFFF)
    return [f"{header:08X} {instr:08X}"]
# ==============================================================================
# TOOL VERIFICATION
# ==============================================================================
def check_tools(need_gcc: bool):
    candidates = [AS, LD, OBJCOPY, OBJDUMP, READELF]
    if need_gcc:
        candidates.append(GCC)
    missing = [t for t in candidates if not os.path.isfile(t)]
    if missing:
        print("[ERROR] devkitPPC tools not found:", file=sys.stderr)
        for t in missing:
            print(f"        {t}", file=sys.stderr)
        sys.exit(1)
# ==============================================================================
# LINKER SCRIPT
# ==============================================================================
def make_linker_script(func_name: str) -> str:
    """Place .picdata (.rodata/.data) at address 0 so GCC emits 'lis rN, 0'
    for all static data references — those instructions are then patched to
    'mr rN, r31' at payload-build time."""
    # .bss/.sbss/COMMON are folded INTO the loaded .picdata (not a NOLOAD section)
    # so uninitialized/zero statics get real, zero-filled backing at an r31-relative
    # address and persist for the life of the code — just like initialized statics.
    return (
        "SECTIONS {\n"
        "    . = 0x00000000;\n"
        "    .picdata : {\n"
        "        *(.rodata*)\n"
        "        *(.data*)\n"
        "        *(.sdata*)\n"
        "        *(.sdata2*)\n"
        "        *(.sbss*)\n"
        "        *(.bss*)\n"
        "        *(COMMON)\n"
        "    }\n"
        "    . = ALIGN(4);\n"
        "    .text : {\n"
        f"        *(.text.{func_name})\n"
        "        *(.text*)\n"
        "    }\n"
        "    /DISCARD/ : {\n"
        "        *(.cgecko_hooks) *(.comment) *(.gnu.attributes)\n"
        "        *(.eh_frame*) *(.pdr)\n"
        "    }\n"
        "}\n"
    )
# ==============================================================================
# COMPILATION & ASSEMBLY & LINKING
# ==============================================================================
def compile_c(c_path: str, obj_path: str, debug: bool):
    cmd = [GCC] + GCC_FLAGS + [c_path, "-o", obj_path]
    if debug:
        print(f"[DEBUG] Compile: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stderr.strip():
        print("[COMPILER]\n" + result.stderr)
    if result.returncode != 0:
        die("Compilation failed.")
def assemble_asm(asm_path: str, obj_path: str, debug: bool):
    cmd = [AS] + AS_FLAGS + [asm_path, "-o", obj_path]
    if debug:
        print(f"[DEBUG] Assemble: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stderr.strip():
        print("[ASSEMBLER]\n" + result.stderr)
    if result.returncode != 0:
        die("Assembly failed.")
def get_libgcc() -> str:
    """Ask GCC where its libgcc.a is for linking."""
    result = subprocess.run(
        [GCC, "-mcpu=750", "-meabi", "-mhard-float", "-print-libgcc-file-name"],
        capture_output=True, text=True
    )
    return result.stdout.strip()
def link_elf(obj_path: str, elf_path: str, ld_path: str, debug: bool, entry: str | None = None):
    libgcc = get_libgcc()
    cmd    = [LD, "-T", ld_path, "--nostdlib"]
    if entry:
        cmd += ["--gc-sections", f"--entry={entry}"]
    cmd   += [obj_path, libgcc, "-o", elf_path]
    if debug:
        print(f"[DEBUG] Link: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stderr.strip():
        print("[LINKER]\n" + result.stderr)
    if result.returncode != 0:
        die("Linking failed.")
# ==============================================================================
# SECTION EXTRACTION
# ==============================================================================
def elf_section_size(elf_path: str, name: str) -> int:
    """Memory size (bytes) of a section, from readelf -S. This is the full extent
    INCLUDING any zero-filled .bss tail folded into the section — objcopy's binary
    output can omit that tail, so callers pad up to this size."""
    out = subprocess.run([READELF, "-SW", elf_path],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        m = re.search(re.escape(name) + r"\s+\w+\s+[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+([0-9A-Fa-f]+)",
                      line)
        if m:
            return int(m.group(1), 16)
    return 0
def extract_section(obj_path: str, section: str) -> bytes:
    tmp = obj_path + ".sec.tmp"
    try:
        result = subprocess.run(
            [OBJCOPY, "-O", "binary", "--only-section", section, obj_path, tmp],
            capture_output=True
        )
        if result.returncode != 0 or not os.path.isfile(tmp):
            return b""
        with open(tmp, "rb") as f:
            return f.read()
    except Exception:
        return b""
    finally:
        if os.path.isfile(tmp):
            os.remove(tmp)
# ==============================================================================
# FPU DETECTION
# ==============================================================================
FP_OPCODES = {48, 49, 50, 51, 52, 53, 54, 55, 59, 63}
def detect_used_fprs(text: bytes, extra_fprs: set[int], debug: bool) -> set[int]:
    used = set(extra_fprs)
    for i in range(len(text) // 4):
        word   = struct.unpack_from(">I", text, i * 4)[0]
        opcode = (word >> 26) & 0x3F
        fpr    = (word >> 21) & 0x1F
        if opcode in FP_OPCODES:
            used.add(fpr)
    if used:
        names = ", ".join(f"f{n}" for n in sorted(used))
        print(f"[INFO] FPU registers: {names}")
    return used
# ==============================================================================
# BACKUP / RESTORE
# ==============================================================================
FRAME_GPR = 0x100
FPR_SLOT_SIZE = 8
FPR_BASE_OFFSET = 0x100
def compute_frame_size(used_fprs: set[int]) -> int:
    if not used_fprs:
        return FRAME_GPR
    raw = FPR_BASE_OFFSET + ((max(used_fprs) + 1) * FPR_SLOT_SIZE)
    return (raw + 15) & ~15
def _stfd(f, o): return (54 << 26) | (f << 21) | (1 << 16) | (o & 0xFFFF)
def _lfd (f, o): return (50 << 26) | (f << 21) | (1 << 16) | (o & 0xFFFF)
def _stw (r, o): return (36 << 26) | (r << 21) | (1 << 16) | (o & 0xFFFF)
def _lwz (r, o): return (32 << 26) | (r << 21) | (1 << 16) | (o & 0xFFFF)
def _stwu(r, o): return (37 << 26) | (r << 21) | (1 << 16) | (o & 0xFFFF)
def _addi(d, s, i): return (14 << 26) | (d << 21) | (s << 16) | (i & 0xFFFF)
def _stmw(r, o): return (47 << 26) | (r << 21) | (1 << 16) | (o & 0xFFFF)
def _lmw (r, o): return (46 << 26) | (r << 21) | (1 << 16) | (o & 0xFFFF)
def _mr(d, s): return (31 << 26) | (s << 21) | (d << 16) | (s << 11) | (444 << 1)
def _pack(*ii): return b"".join(struct.pack(">I", i) for i in ii)
def build_backup(frame_size: int, used_fprs: set[int]) -> bytes:
    instrs = [
        MFLR_R0,
        _stw(0, 0x4),
        _stwu(1, -frame_size),
        _stmw(3, 0x8),
        _mr(30, 1),
    ]
    for n in sorted(used_fprs):
        instrs.append(
            _stfd(n, FPR_BASE_OFFSET + n * FPR_SLOT_SIZE)
        )
    return _pack(*instrs)
def build_restore(frame_size: int, used_fprs: set[int]) -> bytes:
    instrs = []
    for n in sorted(used_fprs):
        instrs.append(
            _lfd(n, FPR_BASE_OFFSET + n * FPR_SLOT_SIZE)
        )
    instrs += [
        _lmw(3, 0x8),
        _lwz(0, frame_size + 0x4),
        _addi(1, 1, frame_size),
        MTLR_R0,
    ]
    return _pack(*instrs)
# ==============================================================================
# PAYLOAD ASSEMBLY
# ==============================================================================
# Opcodes where bits 25-21 encode a destination GPR that is written.
# Stores (36-39,44-45,47,52-55), compares (10-11), and FP loads (48-51)
# are excluded — for stores bits 25-21 are rS (source), not rD.
_GPR_WRITE_OPS = {
    7, 8, 12, 13, 14, 15,           # mulli subfic addic addic. addi addis
    20, 21, 23,                      # rlwimi rlwinm rlwnm
    24, 25, 26, 27, 28, 29,          # ori oris xori xoris andi. andis.
    31,                              # extended integer ops (add sub and or mr …)
    32, 33, 34, 35, 40, 41, 42, 43, # integer loads: lwz lwzu lbz lbzu lhz lhzu lha lhau
}
_BRANCH_OPS = {16, 18, 19}
def patch_lis_for_pic(text: bytes) -> bytes:
    """Eliminate every 'lis rN, 0' emitted for .picdata access.
    GCC links .picdata at address 0 and emits 'lis rN, 0' as the upper-half
    load for each static data address.  We nop the lis in place and
    propagate r31 into the rA field of downstream instructions that used rN
    as a base — until rN is redefined or a branch is reached.
    Result: 'lis r9, 0 / lfs f1, 0(r9)' becomes 'lfs f1, 0(r31)'."""
    words = list(struct.unpack(f">{len(text)//4}I", text))
    to_remove = set()
    for i, word in enumerate(words):
        if (word & 0xFC1FFFFF) != 0x3C000000:
            continue
        rN = (word >> 21) & 0x1F
        for j in range(i + 1, min(i + 17, len(words))):
            w  = words[j]
            op = (w >> 26) & 0x3F
            rA = (w >> 16) & 0x1F
            rD = (w >> 21) & 0x1F
            if op in _BRANCH_OPS:
                break
            if rA == rN:
                words[j] = (w & ~(0x1F << 16)) | (31 << 16)
            if rD == rN and op in _GPR_WRITE_OPS:
                break
        to_remove.add(i)
    # Overwrite each dead 'lis rN, 0' with a nop IN PLACE. Do NOT delete it:
    # removing a word shifts every later instruction down by 4, and nothing here
    # relocates branch targets, so any branch straddling the deletion lands 4 bytes
    # off (per deletion). That silently corrupts control flow whenever .picdata exists.
    NOP = 0x60000000  # ori r0, r0, 0
    for idx in to_remove:
        words[idx] = NOP
    if to_remove:
        print(f"[INFO] PIC: nop'd {len(to_remove)} 'lis rN, 0', base uses → r31")
    return struct.pack(f">{len(words)}I", *words)
def pad_and_terminate(payload: bytes,
                       appended_instr: int | None,
                       debug: bool) -> bytes:
    """
    Append the optional overwritten instruction, then pad to C2 alignment.
    Layout: [...payload...] [instr?] [last_instr|nop] [00000000]
    The final 00000000 is overwritten at runtime by the gecko handler
    with a branch back to the instruction AFTER the injection site.
    If // Instruction is given, it is placed just before the terminator
    so it executes as the last thing before the handler branches back.
    """
    if appended_instr is not None:
        payload += struct.pack(">I", appended_instr)
    n = len(payload) // 4
    if n % 2 == 1:
        payload += struct.pack(">I", TERMINATOR)
    else:
        payload += struct.pack(">II", NOP_INSTR, TERMINATOR)
    if debug:
        print(f"[DEBUG] Final: {len(payload)//4} instructions ({len(payload)} bytes)")
    return payload
# ==============================================================================
# DISASSEMBLY
# ==============================================================================
def disassemble(obj_path: str) -> str:
    return subprocess.run(
        [OBJDUMP, "-d", "-M", "powerpc", obj_path],
        capture_output=True, text=True
    ).stdout
# ==============================================================================
# UTILITIES
# ==============================================================================
def die(msg: str) -> NoReturn:
    print(f"[ERROR] {msg}", file=sys.stderr)
    if sys.stdin.isatty():
        input("\nPress Enter to close...")
    sys.exit(1)
def warn(msg: str):
    print(f"[WARN]  {msg}", file=sys.stderr)
# ==============================================================================
# DOLPHIN INI DEPLOY
# ==============================================================================
def deploy_to_ini(ini_path: str, name: str, gecko_code: str, enable: bool = True, force: bool = False):
    code_lines = gecko_code.strip().splitlines()
    new_header = code_lines[0]
    new_body   = code_lines[1:]
    if os.path.isfile(ini_path):
        with open(ini_path, "r", encoding="utf-8") as f:
            raw = f.read()
        if not raw.strip():
            raw = "[Gecko]\n\n[Gecko_Enabled]\n"
    else:
        raw = "[Gecko]\n\n[Gecko_Enabled]\n"
    lines = raw.splitlines()
    def ensure_section(tag: str) -> int:
        for i, l in enumerate(lines):
            if l.strip().lower() == tag.lower():
                return i
        lines.append("")
        lines.append(tag)
        return len(lines) - 1
    gecko_idx   = ensure_section("[Gecko]")
    enabled_idx = ensure_section("[Gecko_Enabled]")
    gecko_end   = enabled_idx if enabled_idx > gecko_idx else len(lines)
    gecko_body  = lines[gecko_idx + 1 : gecko_end]
    blocks: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for line in gecko_body:
        if line.startswith("$"):
            if current is not None:
                blocks.append(current)
            current = (line, [])
        elif current is not None:
            current[1].append(line)
    if current is not None:
        blocks.append(current)
    def code_name(header: str) -> str:
        h       = header.lstrip("$")
        bracket = h.find("[")
        return (h[:bracket] if bracket != -1 else h).strip()
    target_name = code_name(new_header)
    found = False
    for i, (hdr, body) in enumerate(blocks):
        if code_name(hdr) == target_name:
            blocks[i] = (new_header, new_body)
            found = True
            print(f"[INFO] Updated existing code '{target_name}' in ini.")
            break
    if not found:
        blocks.append((new_header, new_body))
        print(f"[INFO] Added new code '{target_name}' to ini.")
    new_gecko_lines = ["[Gecko]"]
    for i, (hdr, body) in enumerate(blocks):
        if i != 0:
            new_gecko_lines.append("")
        new_gecko_lines.append(hdr)
        cleaned_body = [line.rstrip("\r\n") for line in body if line.strip()]
        new_gecko_lines.extend(cleaned_body)
    enabled_body  = lines[enabled_idx + 1:]
    enabled_names = [l.strip() for l in enabled_body if l.strip()]
    enabled_entry = f"${target_name}"
    present = enabled_entry in enabled_names
    if force:
        # Apply the requested state no matter what, overriding any existing toggle.
        if enable and not present:
            enabled_names.append(enabled_entry)
        elif not enable and present:
            enabled_names = [n for n in enabled_names if n != enabled_entry]
    elif enable and not found and not present:
        # Soft: only set the state when adding a NEW code; leave existing toggles alone.
        enabled_names.append(enabled_entry)
    new_enabled_lines = ["", "[Gecko_Enabled]"]
    new_enabled_lines.extend(enabled_names)
    pre_gecko   = lines[:gecko_idx]
    final_lines = pre_gecko + new_gecko_lines + new_enabled_lines
    with open(ini_path, "w", encoding="utf-8") as f:
        f.write("\n".join(final_lines) + "\n")
    print(f"[INFO] ini updated: {ini_path}")
# ==============================================================================
# DOLPHIN LAUNCH
# ==============================================================================
def launch_dolphin(dolphin_path: str, iso_path: str):
    """Launch Dolphin emulator with the given ISO."""
    if not os.path.isfile(dolphin_path):
        warn(f"Dolphin executable not found: {dolphin_path}")
        return
    if not os.path.isfile(iso_path):
        warn(f"ISO not found: {iso_path}")
        return
    print(f"[INFO] Launching Dolphin...")
    subprocess.Popen([dolphin_path, "-b", "-e", iso_path])
# ==============================================================================
# C0 (EXECUTE-ASM) SUPPORT
# ==============================================================================
# A C0 code is bl'd into by the codehandler once per frame. It has no injection
# address and no overwritten instruction; it must simply end in a real blr to
# return to the handler. Detection rule: any code that does NOT follow an
# // Address: comment is the C0 — i.e. the leading region (everything before the
# first // Address:) becomes the C0, each Address->next-Address slice a C2. At
# most one C0 per file, pinned to the top. Everything ships under one $basename /
# one toggle, exactly as before.

def format_c0(payload: bytes) -> list[str]:
    """Format payload as a C0 code. NNNNNNNN is the number of 8-byte lines that
    follow, including the terminating blr/padding line."""
    assert len(payload) % 8 == 0
    lines = [f"C0000000 {len(payload)//8:08X}"]
    for i in range(0, len(payload), 8):
        w1, w2 = struct.unpack(">II", payload[i:i+8])
        lines.append(f"{w1:08X} {w2:08X}")
    return lines


def terminate_c0(payload: bytes) -> bytes:
    """Pad a *compiled* C0 payload to a whole 8-byte line. The C body already
    ends in the compiler-emitted blr (non-naked epilogue / leaf return), so we
    add ONLY the padding word when the instruction count is odd — never a blr."""
    if (len(payload) // 4) % 2:
        payload += struct.pack(">I", TERMINATOR)
    return payload


def finalize_c0_asm(text: bytes) -> bytes:
    """Finalize a hand-written ASM C0: ensure it ends in blr (append + warn if
    the author forgot), then pad to a whole 8-byte line. blrs are NOT redirected
    here — every blr in a C0 is a genuine return to the codehandler."""
    words = list(struct.unpack(f">{len(text)//4}I", text))
    if words[-1] != BLR_INSTR:
        warn("C0 ASM does not end in 'blr' — CGecko appended one for you. "
             "Verify your control flow is meant to return at the end of the block.")
        words.append(BLR_INSTR)
    if len(words) % 2:
        words.append(TERMINATOR)
    return struct.pack(f">{len(words)}I", *words)


def _build_c0_asm(section: dict, idx: int, tmpdir: str, debug: bool) -> list[str]:
    """Assemble the leading-region ASM into a C0 code. Returns [] if the region
    held only directives/comments (no emitted instructions)."""
    tag      = f"c0_{idx}"
    src_path = os.path.join(tmpdir, f"{tag}.s")
    obj_path = os.path.join(tmpdir, f"{tag}.o")
    with open(src_path, "w") as f:
        f.write(section["source"])
    assemble_asm(src_path, obj_path, debug)
    text = extract_section(obj_path, ".text")
    if not text:
        if debug:
            print("[DEBUG] C0 (ASM) leading region emitted no code — skipping.")
        return []
    payload = finalize_c0_asm(text)
    lines   = format_c0(payload)
    print(f"[INFO]   C0 (ASM) -> {len(payload) // 8} line(s)")
    return lines


def _build_section(section: dict, idx: int, tmpdir: str,
                   is_asm: bool, debug: bool) -> list[str]:
    """Dispatch a parsed ASM section to the correct builder by type. (C files go
    through generate_c_codes / .cgecko_hooks; this path is ASM-only.)"""
    if section["type"] == "c0":
        return _build_c0_asm(section, idx, tmpdir, debug)
    return _build_asm_section(section, idx, tmpdir, debug)


def _asm_has_content(text: str) -> bool:
    """True if the ASM slice has any non-comment, non-blank line."""
    for line in text.splitlines():
        if re.sub(r'(#|//).*$', '', line).strip():
            return True
    return False


def split_codes(source: str, is_asm: bool = True) -> list[dict]:
    """Split an ASM source file into tagged sections in source order.

    Leading region (before the first # Address:) -> a C0 section when it holds
    emitted instructions. Each # Address: -> next-# Address: slice -> a C2 section.
    (C files no longer use this path — their sections come from .cgecko_hooks.)
    """
    matches = list(ADDRESS_PATTERN.finditer(source))
    leading = source[:matches[0].start()] if matches else source

    sections: list[dict] = []

    # ---- C0 (leading region) ----
    if INSTRUCTION_PATTERN.search(leading):
        warn("# Instruction: is ignored in ASM files.")
    if _asm_has_content(leading):
        sections.append({"type": "c0", "address": None,
                         "instruction": None, "source": leading})

    # ---- C2 (Address slices) ----
    for i, m in enumerate(matches):
        start = m.start()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(source)
        section_text = source[start:end]

        addr = int(m.group(1), 16)
        if addr % 4 != 0:
            die(f"Address {hex(addr)} is not 4-byte aligned.")
        if not (0x80000000 <= addr <= 0x81FFFFFF):
            warn(f"Address {hex(addr)} is outside typical GameCube RAM.")

        # A State: inside this slice gates THIS injection only, so one gecko
        # code can mix e.g. a menu-rel hook with game-rel hooks.
        sec_state = parse_state(section_text)

        if INSTRUCTION_PATTERN.search(section_text):
            warn(f"# Instruction: is ignored in ASM files "
                 f"(section at {addr:#010x}).")
        sections.append({"type": "c2", "address": addr,
                         "instruction": None, "source": section_text,
                         "state": sec_state})

    return sections


# ==============================================================================
# C HOOK METADATA  (.cgecko_hooks records emitted by Common.h)
# ==============================================================================
ASM_FLAG           = 0x1
CGECKO_INSTR_MAX   = 64
# struct CGeckoHook: u32 address, u32 state, u32 flags, char[64] instruction, ptr fn
CGECKO_RECORD_SIZE = 4 + 4 + 4 + CGECKO_INSTR_MAX + 4
CGECKO_FN_OFFSET   = 4 + 4 + 4 + CGECKO_INSTR_MAX
# Common.h CGECKO_* state tags -> Project Rio scene-id conditional (addr, value).
CGECKO_STATE_MAP = {
    1: (0x280E877C, 0x00000000),   # CGECKO_BOOT
    2: (0x280E877C, 0x00000004),   # CGECKO_MENU
    3: (0x280E877C, 0x00000005),   # CGECKO_GAME
}
def parse_relocs(obj_path: str, section: str) -> dict[int, str]:
    """Map relocation offset -> target symbol name for one section, read from the
    object's .rela<section>. Used to resolve each CGeckoHook.fn pointer to its entry
    function name (the rest of the record is plain integers / inline chars)."""
    out: dict[int, str] = {}
    txt = subprocess.run([READELF, "-r", obj_path],
                         capture_output=True, text=True).stdout
    want, in_block = f"'.rela{section}'", False
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("Relocation section"):
            in_block = want in s
            continue
        if not in_block or not s or s.startswith("Offset"):
            continue
        parts = s.split()
        try:
            off = int(parts[0], 16)
        except (ValueError, IndexError):
            continue
        if len(parts) >= 5:
            out[off] = parts[4]
    return out
def read_hook_records(obj_path: str) -> list[dict]:
    """Parse CGeckoHook records out of the compiled object's .cgecko_hooks section.
    Returns [] when the section is absent (no Common.h hooks declared)."""
    data = extract_section(obj_path, ".cgecko_hooks")
    if not data:
        return []
    if len(data) % CGECKO_RECORD_SIZE != 0:
        die(f".cgecko_hooks is {len(data)} bytes, not a multiple of "
            f"{CGECKO_RECORD_SIZE} — struct CGeckoHook is out of sync with Common.h.")
    relocs = parse_relocs(obj_path, ".cgecko_hooks")
    hooks: list[dict] = []
    for i in range(len(data) // CGECKO_RECORD_SIZE):
        base    = i * CGECKO_RECORD_SIZE
        address = struct.unpack_from(">I", data, base)[0]
        state   = struct.unpack_from(">I", data, base + 4)[0]
        flags   = struct.unpack_from(">I", data, base + 8)[0]
        instr   = data[base + 12:base + 12 + CGECKO_INSTR_MAX] \
                      .split(b"\x00", 1)[0].decode("ascii", "replace")
        entry   = relocs.get(base + CGECKO_FN_OFFSET)
        if not entry:
            die(f"CGeckoHook #{i}: no fn relocation. A hook's function must be a "
                f"defined, non-static 'void name(void)'.")
        if address:
            if address % 4:
                die(f"Hook '{entry}': address {address:#x} is not 4-byte aligned.")
            if not (0x80000000 <= address <= 0x81FFFFFF):
                warn(f"Hook '{entry}': address {address:#x} outside typical GC RAM.")
        if state and state not in CGECKO_STATE_MAP:
            die(f"Hook '{entry}': unknown state tag {state}. "
                f"Use CGECKO_ALWAYS / CGECKO_BOOT / CGECKO_MENU / CGECKO_GAME.")
        hooks.append({"address": address, "state": state, "flags": flags,
                      "instruction": instr or None, "entry": entry})
    return hooks
def strip_section(obj_in: str, obj_out: str, section: str):
    """Copy an object with one section (and its relocations) removed, so build-
    time metadata is gone before per-hook --gc-sections linking."""
    subprocess.run([OBJCOPY, "--remove-section", section,
                    "--remove-section", ".rela" + section, obj_in, obj_out],
                   capture_output=True)
    if not os.path.isfile(obj_out):
        shutil.copyfile(obj_in, obj_out)
def build_call_payload(elf_path: str, entry: str, is_c0: bool,
                       appended_instr: int | None, flags: int, debug: bool) -> bytes:
    """Assemble the call-model payload for one C hook.

        BACKUP                                 ; save game regs; r30 = frame base
        [ bl +picdata ; picdata ; mflr r31 ]   ; PIC base, when static data exists
        bl   entry                             ; call the hook (LR -> RESTORE)
        RESTORE                                ; restore game regs / LR
        b tail   (C2)   |   blr   (C0)         ; return to game / to codehandler
        entry: [.text]                        ; the hook; its blr returns to RESTORE
        tail:  [instruction?] [pad] 00000000

    Because *we* issue the `bl`, the hook's `blr` returns into the payload (to
    RESTORE), never to the game's LR — so no blr rewriting is needed and helper
    functions just work. The entry is placed first in .text by the linker script,
    so `bl entry` reaches it at a fixed distance past RESTORE.

    (ASM hooks never reach here — build_c_hook routes them to build_asm_hook.)"""
    text = extract_section(elf_path, ".text")
    if not text:
        die(f"Hook '{entry}' produced no .text — is the function empty?")
    # Size, not extracted bytes: an all-.bss data table is NOBITS, so objcopy emits
    # nothing for it even though the statics need real, zero-filled r31 storage.
    picsize = elf_section_size(elf_path, ".picdata")
    if picsize:
        picdata = extract_section(elf_path, ".picdata")
        if len(picdata) < picsize:
            picdata += b"\x00" * (picsize - len(picdata))   # zero-fill .bss tail
        picdata += b"\x00" * ((-len(picdata)) % 4)           # 4-byte align mflr r31
        text     = patch_lis_for_pic(text)
    else:
        picdata = b""
    used_fprs  = detect_used_fprs(text, set(), debug)
    frame_size = compute_frame_size(used_fprs)
    backup     = build_backup(frame_size, used_fprs)
    restore    = build_restore(frame_size, used_fprs)
    # `bl entry` spans itself (4) + RESTORE + the tail branch (4) to reach .text.
    bl_word = struct.pack(">I", BL_BASE | ((8 + len(restore)) & 0x03FFFFFC))
    if is_c0:
        after_restore = struct.pack(">I", BLR_INSTR)                    # -> codehandler
    else:
        after_restore = struct.pack(">I", B_BASE | ((4 + len(text)) & 0x03FFFFFC))  # skip body
    if picdata:
        pic = (struct.pack(">I", BL_BASE | ((4 + len(picdata)) & 0x03FFFFFC))
               + picdata + struct.pack(">I", MFLR_R31))
    else:
        pic = b""
    payload = backup + pic + bl_word + restore + after_restore + text
    if is_c0:
        payload = terminate_c0(payload)
    else:
        payload = pad_and_terminate(payload, appended_instr, debug)
    if len(payload) % 8 != 0:
        die(f"Hook '{entry}': payload {len(payload)} bytes not 8-byte aligned.")
    return payload
def build_asm_hook(elf_path: str, entry: str, addr: int, is_c0: bool,
                   appended_instr: int | None, debug: bool) -> list[str]:
    """Emit an ASM hook VERBATIM — no BACKUP/RESTORE wrapper, no PIC, no blr
    rewriting. The hook's .text (hand-written asm from ASM_CODE) is the
    payload, exactly like a .asm injection: a C2 runs inline and falls through to
    the handler's branch-back; a C0 must end in blr to return to the codehandler."""
    text = extract_section(elf_path, ".text")
    if not text:
        die(f"ASM hook '{entry}' produced no .text — did you provide its body "
            f"with ASM_CODE({entry}, \"...\")?")
    if is_c0:
        payload = finalize_c0_asm(text)          # ensure trailing blr + pad to 8B
        print(f"[INFO]   {entry}()  ->  C0 asm ({len(payload)//8} lines)")
        return format_c0(payload)
    if len(text) == 4 and appended_instr is None:
        print(f"[INFO]   {entry}()  ->  04 write (asm @ {addr:#010x})")
        return format_04(addr, struct.unpack(">I", text)[0])
    payload = pad_and_terminate(text, appended_instr, debug)
    print(f"[INFO]   {entry}()  ->  {'C3' if addr >= 0x81000000 else 'C2'} asm "
          f"@ {addr:#010x} ({len(payload)//8} lines)")
    return format_c2(addr, payload)
def build_c_hook(hook: dict, idx: int, obj_path: str, tmpdir: str, debug: bool) -> list[str]:
    """Link one hook independently out of the shared object (gc-sections rooted
    at its entry) and format it as a C2/C3 injection or a C0 per-frame code."""
    entry = hook["entry"]
    addr  = hook["address"]
    is_c0 = (addr == 0)
    tag   = f"hook{idx}_{entry}"
    elf   = os.path.join(tmpdir, tag + ".elf")
    ld    = os.path.join(tmpdir, tag + ".ld")
    with open(ld, "w") as f:
        f.write(make_linker_script(entry))
    link_elf(obj_path, elf, ld, debug, entry=entry)
    if debug:
        print(f"[DEBUG] Hook '{entry}' disassembly:\n" + disassemble(elf))
    appended = None
    if hook["instruction"]:
        if is_c0:
            warn(f"Hook '{entry}': per-frame codes have no injection site; "
                 f".instruction is ignored.")
        else:
            appended = assemble_instruction(hook["instruction"], tmpdir)
            print(f"[INFO]   instruction : {hook['instruction']}  ->  {appended:#010x}")
    if hook["flags"] & ASM_FLAG:
        return build_asm_hook(elf, entry, addr, is_c0, appended, debug)
    payload = build_call_payload(elf, entry, is_c0, appended, hook["flags"], debug)
    if is_c0:
        print(f"[INFO]   {entry}()  ->  C0 ({len(payload)//8} lines)")
        return format_c0(payload)
    lines = format_c2(addr, payload)
    print(f"[INFO]   {entry}()  ->  {'C3' if addr >= 0x81000000 else 'C2'} "
          f"@ {addr:#010x} ({len(payload)//8} lines)")
    return lines
def generate_c_codes(c_path: str, tmpdir: str, debug: bool) -> list[str]:
    """Compile a C file once, read its Common.h hook records, and build one gecko
    code per hook. Per-hook state gates wrap each code independently."""
    obj = os.path.join(tmpdir, "unit.o")
    compile_c(c_path, obj, debug)
    hooks = read_hook_records(obj)
    if not hooks:
        die("No CGECKO_HOOK / CGECKO_FRAME records found. Declare each hook "
            "(Common.h is included for you), e.g.\n"
            "    CGECKO_HOOK(my_hook, .address = 0x80234567, .state = CGECKO_GAME);\n"
            "    void my_hook(void) { ... }")
    stripped = os.path.join(tmpdir, "unit.stripped.o")
    strip_section(obj, stripped, ".cgecko_hooks")
    n_c0 = sum(1 for h in hooks if not h["address"])
    n_c2 = len(hooks) - n_c0
    print(f"[INFO] Hooks          : {len(hooks)}  ({n_c0} C0, {n_c2} C2)")
    all_lines: list[str] = []
    for i, hook in enumerate(hooks):
        print(f"[INFO] Hook {i + 1}/{len(hooks)} : {hook['entry']}()")
        lines = build_c_hook(hook, i, stripped, tmpdir, debug)
        gate  = CGECKO_STATE_MAP.get(hook["state"])
        if gate:
            a, v  = gate
            lines = [f"{a:08X} {v:08X}"] + lines + ["E2000001 00000000"]
            print(f"[INFO]   state gate  : {a:#010x} {v:#010x}")
        all_lines.extend(lines)
    return all_lines
# ==============================================================================
# ENTRY POINT
# ==============================================================================
def main():
    # Status output uses Unicode (→, em-dashes); force UTF-8 so it can't crash on
    # a legacy Windows console codepage (cp1252) when the PIC/arrow lines print.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass
    parser = argparse.ArgumentParser(
        description="Convert a .c or .asm file into a C2/C3 Gecko code for GameCube modding."
    )
    parser.add_argument("input", nargs="?",
                        help="Input .c or .asm file. If omitted, uses build_file from config.json.")
    parser.add_argument("-d", action="store_true",
                        help="Debug mode: verbose output, save build artifacts")
    # Post-build enabled state in the ini. With neither flag the behavior is
    # unchanged: a newly added code is enabled, and an existing code keeps its
    # current toggle. --enabled / --disabled set the final state outright,
    # overriding an existing code's toggle either way.
    state = parser.add_mutually_exclusive_group()
    state.add_argument("--enabled", dest="enable_mode", action="store_const", const="enabled",
                       help="Make the code enabled after building, overriding any existing toggle.")
    state.add_argument("--disabled", dest="enable_mode", action="store_const", const="disabled",
                       help="Make the code disabled after building, overriding any existing toggle.")
    state.add_argument("--no-enable", dest="enable_mode", action="store_const", const="no_enable",
                       help="Legacy: don't enable a newly added code, but leave existing toggles alone.")
    parser.add_argument("--no-launch", action="store_true",
                        help="Do not launch Dolphin after building, even if config says to")
    args = parser.parse_args()
    debug   = args.d
    # enable_mode: None = default (enable a new code, preserve an existing toggle);
    # "enabled"/"disabled" force that final state; "no_enable" = legacy soft disable
    # (don't enable a new code, but leave existing toggles alone).
    enable = args.enable_mode not in ("disabled", "no_enable")   # None and "enabled" -> True
    force  = args.enable_mode in ("enabled", "disabled")
    do_launch = get_launch() and not args.no_launch
    input_arg = args.input
    if input_arg is None:
        input_arg = get_build_file()
        if input_arg:
            print(f"[INFO] Using build_file from config: {input_arg}")
        else:
            input_arg = find_latest_source()
            if input_arg is None:
                die("No input given, no 'build_file' in config.json, and no "
                    ".c/.asm/.ini files found under the project directory.")
            print(f"[INFO] Auto-selected newest source: {input_arg}")
    c_path = os.path.abspath(input_arg)
    if not os.path.isfile(c_path):
        die(f"Input file not found: {c_path}")
    ext = os.path.splitext(c_path)[1].lower()
    if ext not in (".c", ".asm", ".ini"):
        die(f"Unsupported file extension '{ext}'. Expected .c, .asm, or .ini")
    is_asm   = (ext == ".asm")
    is_ini   = (ext == ".ini")
    raw_mode = is_asm
    # utf-8: sources routinely carry box-drawing/em-dash comments, which the
    # legacy Windows default codepage (cp1252) cannot decode.
    with open(c_path, "r", encoding="utf-8") as f:
        source = f.read()
    if is_ini:
        blocks = parse_gecko_blocks(source)
        if not blocks:
            die("No gecko code blocks found in .ini file.")
        print(f"[INFO] Mode           : INI")
        print(f"[INFO] Codes found    : {len(blocks)}")
        ini_path = get_ini_path()
        if ini_path:
            for blk_name, gecko_code in blocks:
                deploy_to_ini(ini_path, blk_name, gecko_code, enable, force)
            print(f"[INFO] Successfully deployed {len(blocks)} code(s)")
            if do_launch:
                dolphin_path = get_dolphin_path()
                iso_path     = get_iso_path()
                if dolphin_path and iso_path:
                    launch_dolphin(dolphin_path, iso_path)
        else:
            warn("No ini_path in config.json — writing to codes.txt.")
            with open(TXT_PATH, "w", encoding="utf-8") as f:
                f.write("\n\n".join(gc for _, gc in blocks) + "\n")
            print(f"[INFO] Wrote {len(blocks)} code(s) to {TXT_PATH}")
        return
    base_name = os.path.splitext(os.path.basename(c_path))[0]
    name      = base_name                      # gecko code name (preserves spaces)

    author = parse_author(source)              # cosmetic, from a // Author: comment
    notes  = parse_notes(source, is_asm)       # cosmetic, from // * comments

    print(f"[INFO] Mode           : {'ASM' if is_asm else 'C'}")
    print(f"[INFO] Name           : {name}")
    if author:
        print(f"[INFO] Author         : {author}")

    check_tools(need_gcc=not is_asm)

    tmpdir = tempfile.mkdtemp(prefix="c2gecko_")
    try:
        if is_asm:
            # ASM keeps the comment-based directives (# Address:, file-level # State:);
            # it is the raw escape hatch and has no #include / metadata-section story.
            file_state = parse_file_state(source)
            cond_addr, cond_value = file_state if file_state else (None, None)
            if file_state:
                print(f"[INFO] State          : file-level conditional wrapper "
                      f"{file_state[0]:#010x} {file_state[1]:#010x}")
            sections = split_codes(source, is_asm=True)
            if not sections:
                die("No code sections found — need a C0 region (code before the "
                    "first # Address:) or at least one # Address: comment.")
            n_c0 = sum(1 for s in sections if s["type"] == "c0")
            n_c2 = sum(1 for s in sections if s["type"] == "c2")
            print(f"[INFO] Sections       : {len(sections)}  ({n_c0} C0, {n_c2} C2)")
            all_code_lines: list[str] = []
            for i, section in enumerate(sections):
                loc = (f"{section['address']:#010x}" if section["address"] is not None
                       else "leading region")
                sec_state = section.get("state")
                print(f"[INFO] Section {i + 1}/{len(sections)} : "
                      f"{section['type'].upper():3} @ {loc}")
                lines = _build_section(section, i, tmpdir, is_asm, debug)
                if sec_state:
                    s_addr, s_val = sec_state
                    lines = ([f"{s_addr:08X} {s_val:08X}"] + lines
                             + ["E2000001 00000000"])
                all_code_lines.extend(lines)
        else:
            # C: metadata comes from Common.h .cgecko_hooks records (per-hook state gates).
            cond_addr, cond_value = None, None
            all_code_lines = generate_c_codes(c_path, tmpdir, debug)

        if not all_code_lines:
            die("No gecko code lines were generated.")

        gecko_code = build_gecko_output(all_code_lines, name, author, notes,
                                        cond_value, cond_addr)

        ini_path = get_ini_path()
        if ini_path:
            deploy_to_ini(ini_path, name, gecko_code, enable, force)
            print(f"[INFO] Successfully generated '{name}'")
            if do_launch:
                dolphin_path = get_dolphin_path()
                iso_path     = get_iso_path()
                if dolphin_path and iso_path:
                    launch_dolphin(dolphin_path, iso_path)
        else:
            warn("No ini_path in config.json — writing to codes.txt.")
            with open(TXT_PATH, "w", encoding="utf-8") as f:
                f.write(gecko_code + "\n")
            print(f"[INFO] Successfully generated '{name}' -> {TXT_PATH}")
    finally:
        if not debug:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print(f"[DEBUG] Temp dir: {tmpdir}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        die(f"Unexpected error: {exc}")