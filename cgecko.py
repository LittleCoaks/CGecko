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
def _note_pat() -> re.Pattern:
    return re.compile(r"(?://|#)\s*(\*[^\n]*)", re.MULTILINE)
ADDRESS_PATTERN     = re.compile(
    r"(?://|#)\s*(?:Address|Inject|Entry)\s*:\s*(0x[0-9A-Fa-f]{8})",
    re.IGNORECASE | re.MULTILINE
)
AUTHOR_PATTERN      = _pat("Author",      r"(.+?)(?:\n|$)")
INSTRUCTION_PATTERN = _pat("Instruction", r"(.+?)(?:\n|$)")
STATE_PATTERN       = _pat("State",       r"(.+?)(?:\n|$)")
NOTE_PATTERN        = _note_pat()
def make_func_pattern(func_name: str) -> re.Pattern:
    return re.compile(
        r"^([^\S\n]*)"
        r"(?:__attribute__\s*\(\s*\(\s*naked\s*\)\s*\)\s*)?"
        r"((?:unsigned\s+)?(?:int|void|float|double|void\s*\*))"
        rf"\s+{re.escape(func_name)}\s*\(([^)]*)\)",
        re.MULTILINE
    )
def parse_address(source: str) -> int:
    matches = ADDRESS_PATTERN.findall(source)
    if not matches:
        die("No 'Address: 0x80XXXXXX' comment found in source file.")
    if len(matches) > 1:
        die(f"Multiple Address comments found: {matches}. Only one is allowed.")
    addr = int(matches[0], 16)
    if addr % 4 != 0:
        die(f"Address {hex(addr)} is not 4-byte aligned.")
    if not (0x80000000 <= addr <= 0x81FFFFFF):
        warn(f"Address {hex(addr)} is outside typical GameCube RAM (0x80000000-0x81FFFFFF).")
    return addr
def parse_author(source: str) -> str | None:
    m = AUTHOR_PATTERN.search(source)
    return m.group(1).strip() if m else None
def parse_instruction(source: str) -> str | None:
    m = INSTRUCTION_PATTERN.search(source)
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
# Matches the first non-static function signature in a source slice.
# Does not include 'static' in the alternation, so static helpers are skipped.
_FUNC_DEF_PATTERN = re.compile(
    r"^[^\S\n]*"
    r"(?:__attribute__\s*\(\s*\(\s*naked\s*\)\s*\)\s*)?"
    r"(?:unsigned\s+)?(?:int|void|float|double|void\s*\*)"
    r"\s+(\w+)\s*\([^)]*\)",
    re.MULTILINE
)
def find_entry_func(source: str) -> str:
    """Return the name of the first non-static function definition in source.
    Skips declarations (ending in ';') and only returns the name when the
    signature is immediately followed by '{', making it a definition.
    """
    for m in _FUNC_DEF_PATTERN.finditer(source):
        if source[m.end():].lstrip().startswith("{"):
            return m.group(1)
    die("Could not find any function definition in source.")
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
# MULTI-SECTION
# ==============================================================================
def split_asm_sections(source: str) -> list[dict]:
    """Split a multi-injection ASM file into per-section dicts.
    Each section begins at an '// Address:' comment and runs to the next one.
    Per-section // Instruction: is parsed from that section's slice only, so
    each injection site can independently override the appended instruction.
    """
    matches = list(ADDRESS_PATTERN.finditer(source))
    sections = []
    for i, m in enumerate(matches):
        start = m.start()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(source)
        text  = source[start:end]
        addr = int(m.group(1), 16)
        if addr % 4 != 0:
            die(f"Address {hex(addr)} is not 4-byte aligned.")
        if not (0x80000000 <= addr <= 0x81FFFFFF):
            warn(f"Address {hex(addr)} is outside typical GameCube RAM.")
        instr_m = INSTRUCTION_PATTERN.search(text)
        if instr_m:
            die(f"// Instruction: is not supported in ASM files "
                f"(section at {addr:#010x}). Use it only in .c files.")
        sections.append({
            "address":     addr,
            "instruction": None,
            "source":      text,
        })
    return sections
def _build_asm_section(section: dict, idx: int, tmpdir: str, debug: bool) -> list[str]:
    """Assemble one section of a multi-injection ASM file into gecko code lines."""
    addr       = section["address"]
    instr_text = section["instruction"]
    src_text   = section["source"]
    tag      = f"sec{idx}_{addr:08x}"
    src_path = os.path.join(tmpdir, f"{tag}.s")
    obj_path = os.path.join(tmpdir, f"{tag}.o")
    with open(src_path, "w") as f:
        f.write(src_text)
    assemble_asm(src_path, obj_path, debug)
    appended_instr: int | None = None
    if instr_text:
        appended_instr = assemble_instruction(instr_text, tmpdir)
        print(f"[INFO]   Instruction hex : {appended_instr:#010x}")
    payload = extract_section(obj_path, ".text")
    if not payload:
        die(f"Section {idx + 1} at {addr:#010x} produced no .text output.")
    payload = replace_blr(payload, 0, debug)
    if len(payload) == 4 and appended_instr is None:
        instr_word = struct.unpack(">I", payload)[0]
        lines = format_04(addr, instr_word)
        print(f"[INFO]   {addr:#010x} → 04 write")
    else:
        payload = pad_and_terminate(payload, appended_instr, debug)
        lines   = format_c2(addr, payload)
        print(f"[INFO]   {addr:#010x} → C2  ({len(payload) // 4} words)")
    return lines
# ── C ──────────────────────────────────────────────────────────────────────────
def split_c_sections(source: str) -> list[dict]:
    """Split a multi-injection C file into per-section dicts.
    Everything before the first Address/Inject/Entry comment is the preamble
    (shared includes, defines, static helpers) and is prepended to every
    section's source before compilation, so each section compiles independently.
    Each section's entry function is the first non-static function defined
    after its Address/Inject/Entry comment.
    """
    matches  = list(ADDRESS_PATTERN.finditer(source))
    preamble = source[:matches[0].start()]
    sections = []
    for i, m in enumerate(matches):
        start        = m.start()
        end          = matches[i + 1].start() if i + 1 < len(matches) else len(source)
        section_text = source[start:end]
        addr = int(m.group(1), 16)
        if addr % 4 != 0:
            die(f"Address {hex(addr)} is not 4-byte aligned.")
        if not (0x80000000 <= addr <= 0x81FFFFFF):
            warn(f"Address {hex(addr)} is outside typical GameCube RAM.")
        instr_m = INSTRUCTION_PATTERN.search(section_text)
        sections.append({
            "address":      addr,
            "instruction":  instr_m.group(1).strip() if instr_m else None,
            "source":       preamble + section_text,  # full source passed to compile_c
            "section_text": section_text,             # slice used for entry func detection
        })
    return sections
def _build_c_section(section: dict, idx: int, tmpdir: str, debug: bool) -> list[str]:
    """Compile one section of a multi-injection C file into gecko code lines."""
    addr       = section["address"]
    instr_text = section["instruction"]
    func_name  = find_entry_func(section["section_text"])
    tag      = f"sec{idx}_{addr:08x}"
    src_path = os.path.join(tmpdir, f"{tag}.c")
    obj_path = os.path.join(tmpdir, f"{tag}.o")
    elf_path = os.path.join(tmpdir, f"{tag}.elf")
    ld_path  = os.path.join(tmpdir, f"{tag}.ld")
    print(f"[INFO]   Entry function : {func_name}()")
    rewritten = prepare_source(section["source"], func_name)
    if debug:
        print(f"[DEBUG] Section {idx + 1} rewritten source:\n" + rewritten)
    with open(src_path, "w") as f:
        f.write(rewritten)
    compile_c(src_path, obj_path, debug)
    with open(ld_path, "w") as f:
        f.write(make_linker_script(func_name))
    link_elf(obj_path, elf_path, ld_path, debug, entry=func_name)
    disasm = disassemble(elf_path)
    if debug:
        print(f"[DEBUG] Section {idx + 1} disassembly:\n" + disasm)
    appended_instr: int | None = None
    if instr_text:
        appended_instr = assemble_instruction(instr_text, tmpdir)
        print(f"[INFO]   Instruction hex : {appended_instr:#010x}")
    payload = build_payload(elf_path, False, set(), debug, entry=func_name)
    payload = pad_and_terminate(payload, appended_instr, debug)
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
    return (
        "SECTIONS {\n"
        "    . = 0x00000000;\n"
        "    .picdata : {\n"
        "        *(.rodata*)\n"
        "        *(.data*)\n"
        "    }\n"
        "    . = ALIGN(4);\n"
        "    .text : {\n"
        f"        *(.text.{func_name})\n"
        "        *(.text*)\n"
        "    }\n"
        "    .bss (NOLOAD) : { *(.bss*) *(.sbss*) }\n"
        "    /DISCARD/ : {\n"
        "        *(.comment) *(.gnu.attributes)\n"
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
# PIC STUB
# ==============================================================================
PIC_STUB_INSTRS = [MFLR_R0, 0x48000005, 0x7D6802A6, MTLR_R0]
def pic_stub_bytes() -> bytes:
    return _pack(*PIC_STUB_INSTRS)
# ==============================================================================
# BLR REPLACEMENT
# ==============================================================================
def get_entry_extent(elf_path: str, entry: str) -> int | None:
    """Return the byte extent of the entry function within .text (its offset
    from the start of .text plus its size), or None if it can't be determined.
    Used to limit blr rewriting to the entry function: a helper function's blr
    is a genuine return to its caller and must NOT be routed to RESTORE."""
    try:
        sec = subprocess.run([READELF, "-SW", elf_path],
                             capture_output=True, text=True).stdout
        text_addr = None
        for line in sec.splitlines():
            m = re.search(r"\.text\s+\S+\s+([0-9A-Fa-f]+)", line)
            if m:
                text_addr = int(m.group(1), 16)
                break
        if text_addr is None:
            return None
        syms = subprocess.run([READELF, "-sW", elf_path],
                              capture_output=True, text=True).stdout
        for line in syms.splitlines():
            parts = line.split()
            if len(parts) >= 8 and parts[3] == "FUNC" and parts[7] == entry:
                value = int(parts[1], 16)
                size  = int(parts[2], 0)
                if size == 0:
                    return None
                return (value - text_addr) + size
    except Exception:
        pass
    return None
def replace_blr(text: bytes, data_after: int, debug: bool,
                entry_extent: int | None = None) -> bytes:
    """Route returns to RESTORE. When entry_extent is given (C builds), only
    returns inside [0, entry_extent) — the entry function, which the linker
    script places first in .text — are rewritten; helper functions keep their
    blr so they return to their caller. When None (ASM builds), every blr is
    treated as a return to the game, preserving historical behavior."""
    text_len = len(text)
    words    = list(struct.unpack(f">{text_len // 4}I", text))

    def is_return_to_lr(w: int) -> bool:
        # bclrx: primary opcode 19, extended opcode 16, LK=0. This matches blr and
        # every conditional return (beqlr, bnelr, bltlr, ...). It deliberately does
        # NOT match bcctr/bctr (ext op 528) -- those are FUNCTION_ADDRESS calls -- or
        # blrl (LK=1, a call through LR).
        return (w >> 26) == 19 and ((w >> 1) & 0x3FF) == 16 and (w & 1) == 0

    def is_unconditional(bo: int) -> bool:
        return (bo & 0b10100) == 0b10100   # BO = 1z1zz -> branch always

    # Only a trailing UNCONDITIONAL blr can be dropped to fall through to RESTORE,
    # and only when nothing (PIC data) sits between text and RESTORE. When helper
    # functions follow the entry, the trailing blr belongs to a helper — keep it.
    drop_tail     = (bool(words) and words[-1] == BLR_INSTR and data_after == 0
                     and (entry_extent is None or entry_extent >= text_len))
    effective_len = text_len - (4 if drop_tail else 0)

    uncond = cond = 0
    for i, word in enumerate(words):
        if not is_return_to_lr(word):
            continue
        instr_offset = i * 4
        if entry_extent is not None and instr_offset >= entry_extent:
            continue  # helper function's return — a real blr to its caller
        delta = (effective_len - instr_offset) + data_after
        bo = (word >> 21) & 0x1F
        bi = (word >> 16) & 0x1F
        if is_unconditional(bo):
            uncond += 1
            if drop_tail and i == len(words) - 1:
                continue  # popped below; falls through to RESTORE naturally
            words[i] = B_BASE | (delta & 0x03FFFFFC)
            if debug:
                print(f"[DEBUG] blr at .text+{instr_offset:#05x} -> b +{delta} (skips {data_after} data bytes)")
        else:
            cond += 1
            # Rewrite the conditional return as a bc with the SAME BO/BI, branching
            # forward to RESTORE so the early-exit path still runs the epilogue.
            if not (0 <= delta <= 0x7FFC):
                die(f"conditional return at .text+{instr_offset:#x} is {delta} bytes from "
                    f"RESTORE — too far to encode as a bc (max 0x7FFC). Simplify the code "
                    f"or split it across sections.")
            words[i] = (16 << 26) | (bo << 21) | (bi << 16) | (delta & 0x0000FFFC)
            if debug:
                print(f"[DEBUG] conditional return (BO={bo}, BI={bi}) at .text+{instr_offset:#05x} "
                      f"-> bc +{delta} to RESTORE")
    if drop_tail:
        words.pop()
        uncond -= 1  # the dropped blr is not a replacement
        if debug:
            print(f"[DEBUG] trailing blr at .text+{effective_len:#05x} — dropped (falls through to RESTORE)")
    total = uncond + cond
    if total:
        print(f"[INFO] Routed {total} return(s) to RESTORE "
              f"({cond} conditional, {uncond} unconditional branch(es)).")
    return struct.pack(f">{len(words)}I", *words)
# ==============================================================================
# SOURCE REWRITING  (C only)
# ==============================================================================
def prepare_source(source: str, func_name: str, naked: bool = True) -> str:
    """
    Move the entry function to the top of the source (after preprocessor lines)
    so GCC places it first in its section. Add forward declarations for helpers.
    """
    pattern = make_func_pattern(func_name)
    m       = pattern.search(source)
    if not m:
        die(f"Could not find '{func_name}()' function definition in the source file.")
    brace_start = source.find("{", m.end())
    if brace_start == -1:
        die("Could not find opening brace of entry function.")
    depth, brace_end = 0, brace_start
    for i, ch in enumerate(source[brace_start:], brace_start):
        if ch == "{":   depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                brace_end = i
                break
    else:
        die("Could not find closing brace of entry function.")
    ret_type  = m.group(2).strip()
    args      = m.group(3)
    body      = source[brace_start : brace_end + 1]
    if naked:
        func_text = (f"{m.group(1)}__attribute__((naked)) "
                     f"{ret_type} {func_name}({args}) {body}")
    else:
        func_text = f"{m.group(1)}{ret_type} {func_name}({args}) {body}"
    source_without = source[:m.start()] + source[brace_end + 1:]
    # Insertion point: where the entry function was in the original source.
    # Everything before it (includes, defines, static vars) stays in place.
    insert_pos = m.start()
    # Forward declare all helper functions so the entry function can call them
    fwd_decls = ""
    for fm in re.finditer(
        r'((?:unsigned\s+)?(?:int|void|float|double|void\s*\*))\s+(\w+)\s*\(([^)]*)\)\s*\{',
        source_without[insert_pos:]
    ):
        fwd_ret  = fm.group(1).strip()
        fwd_name = fm.group(2)
        fwd_args = fm.group(3)
        if fwd_name != func_name:
            fwd_decls += f"static {fwd_ret} {fwd_name}({fwd_args});\n"
    rewritten = (source_without[:insert_pos]
                 + fwd_decls
                 + "\n" + func_text + "\n\n"
                 + source_without[insert_pos:])
    return rewritten
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
def build_payload(elf_path: str,
                  raw_mode: bool,
                  extra_fprs: set[int],
                  debug: bool,
                  entry: str | None = None) -> bytes:
    """Assemble the final C2 payload bytes.
    PIC layout when .picdata is non-empty:
        BACKUP
        bl +(4 + len(picdata))   ← LR = &picdata[0]; jumps to mflr r31
        [picdata]                ← .rodata/.data bytes, padded to 4B
        mflr r31                 ← r31 = LR = &picdata[0]
        [.text, lis rN,0 removed; base uses → r31]
        RESTORE
    """
    text    = extract_section(elf_path, ".text")
    picdata = extract_section(elf_path, ".picdata")
    if not text:
        die("No .text section in compiled output. Is the source file empty?")
    # Pad picdata to 4-byte boundary so mflr r31 is 4-byte aligned
    if picdata:
        pad     = b"\x00" * ((-len(picdata)) % 4)
        picdata = picdata + pad
    if debug:
        print(f"[DEBUG] .text    : {len(text)//4} instructions ({len(text)} bytes)")
        print(f"[DEBUG] .picdata : {len(picdata)} bytes")
    if raw_mode:
        return replace_blr(text, 0, debug)
    entry_extent = get_entry_extent(elf_path, entry) if entry else None
    if entry and entry_extent is None:
        warn(f"Could not measure entry '{entry}' in the ELF — routing EVERY blr "
             f"to RESTORE. Helper function returns will be broken; inline them.")
    elif entry_extent is not None and debug:
        print(f"[DEBUG] Entry extent: {entry_extent:#x} bytes "
              f"(blrs past this stay as helper returns)")
    # Patch lis rN,0 → nop + base propagation before blr replacement
    if picdata:
        text = patch_lis_for_pic(text)
    text = replace_blr(text, 0, debug, entry_extent)
    used_fprs  = detect_used_fprs(text, extra_fprs, debug)
    frame_size = compute_frame_size(used_fprs)
    backup     = build_backup(frame_size, used_fprs)
    restore    = build_restore(frame_size, used_fprs)
    if debug:
        fpu_desc = ("GPR only" if not used_fprs else
                    f"GPR+FPU {', '.join(f'f{n}' for n in sorted(used_fprs))}")
        print(f"[DEBUG] Frame : {frame_size:#x} ({fpu_desc})")
    if picdata:
        # bl delta: from bl instruction to mflr r31 (just past picdata)
        # bl at P → LR = P+4 = &picdata[0]; PC = P + bl_delta = &mflr r31
        bl_delta = 4 + len(picdata)
        bl_instr = struct.pack(">I", BL_BASE | (bl_delta & 0x03FFFFFC))
        mflr_r31 = struct.pack(">I", MFLR_R31)
        payload  = backup + bl_instr + picdata + mflr_r31 + text + restore
        if debug:
            print(f"[DEBUG] PIC bl delta: {bl_delta} ({len(picdata)}B data + 4B mflr)")
    else:
        payload = backup + text + restore
    if len(payload) % 4 != 0:
        die(f"Payload size {len(payload)} is not 4-byte aligned.")
    return payload
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


def build_c0_payload(elf_path: str, debug: bool) -> bytes:
    """Build a C0 payload from compiled C.

    No BACKUP/RESTORE wrapper: the codehandler already saves/restores the whole
    game context around the entire code list, and a non-naked compile lets GCC
    set up its own frame/LR only when the body calls something. blrs are left
    intact (replace_blr is NOT run).

    PIC carve-out: when .picdata exists, the injected 'bl' that loads r31 would
    clobber LR (the handler's return address). Bracket it with mflr r0 / mtlr r0
    so the handler return survives into the function's own prologue:

        mflr r0                ; r0 = handler return address
        bl   +(4 + len(data))  ; LR = &picdata; branch past data to mflr r31
        [picdata]
        mflr r31               ; r31 = &picdata  (PIC base)
        mtlr r0                ; LR = handler return (restored)
        [.text]                ; leaf  -> blr returns to handler
                               ; caller -> prologue's mflr reads the correct LR
    """
    text    = extract_section(elf_path, ".text")
    picdata = extract_section(elf_path, ".picdata")
    if not text:
        die("No .text in compiled C0 output. Is the C0 function empty?")
    if picdata:
        picdata = picdata + b"\x00" * ((-len(picdata)) % 4)   # 4-byte align
        text    = patch_lis_for_pic(text)
        bl_delta = 4 + len(picdata)
        stub = (struct.pack(">I", MFLR_R0)
                + struct.pack(">I", BL_BASE | (bl_delta & 0x03FFFFFC))
                + picdata
                + struct.pack(">I", MFLR_R31)
                + struct.pack(">I", MTLR_R0))
        payload = stub + text
        if debug:
            print(f"[DEBUG] C0 PIC: mflr r0 / bl +{bl_delta} / "
                  f"[{len(picdata)}B data] / mflr r31 / mtlr r0")
    else:
        payload = text
    if len(payload) % 4 != 0:
        die(f"C0 payload size {len(payload)} is not 4-byte aligned.")
    return payload


def _build_c0_c(section: dict, idx: int, tmpdir: str, debug: bool) -> list[str]:
    """Compile the leading-region C function into a C0 code.

    Built like a C2 (naked body + BACKUP/RESTORE wrapper via build_payload) so
    the codehandler's working registers (r3-r31) survive the body. A function
    call clobbers every volatile register, and the codehandler keeps codelist
    state in some of them, so without the wrapper the handler dies after the C0
    returns. The only difference from a C2 is the tail: instead of a handler-
    overwritten terminator branching back to an inject site, the C0 ends in a
    real blr to return to the codehandler."""
    func_name = section["entry"]
    tag      = f"c0_{idx}"
    src_path = os.path.join(tmpdir, f"{tag}.c")
    obj_path = os.path.join(tmpdir, f"{tag}.o")
    elf_path = os.path.join(tmpdir, f"{tag}.elf")
    ld_path  = os.path.join(tmpdir, f"{tag}.ld")
    print(f"[INFO]   C0 entry func  : {func_name}()")
    rewritten = prepare_source(section["source"], func_name)   # naked; wrapper supplies the frame
    if debug:
        print(f"[DEBUG] C0 rewritten source:\n" + rewritten)
    with open(src_path, "w") as f:
        f.write(rewritten)
    compile_c(src_path, obj_path, debug)
    with open(ld_path, "w") as f:
        f.write(make_linker_script(func_name))
    link_elf(obj_path, elf_path, ld_path, debug, entry=func_name)
    if debug:
        print(f"[DEBUG] C0 disassembly:\n" + disassemble(elf_path))
    payload = build_payload(elf_path, False, set(), debug, entry=func_name)  # BACKUP + body(blr->RESTORE) + RESTORE
    payload = payload + struct.pack(">I", BLR_INSTR)           # return to the codehandler
    payload = terminate_c0(payload)                            # pad to a whole 8-byte line
    lines   = format_c0(payload)
    print(f"[INFO]   C0 -> {len(payload) // 8} line(s)")
    return lines


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
    """Dispatch a parsed section to the correct builder by type and language."""
    if section["type"] == "c0":
        return (_build_c0_asm if is_asm else _build_c0_c)(section, idx, tmpdir, debug)
    return (_build_asm_section if is_asm else _build_c_section)(section, idx, tmpdir, debug)


def _asm_has_content(text: str) -> bool:
    """True if the ASM slice has any non-comment, non-blank line."""
    for line in text.splitlines():
        if re.sub(r'(#|//).*$', '', line).strip():
            return True
    return False


def _leading_c_entry(leading: str) -> str | None:
    """First non-static function *definition* in the leading region, or None.
    (_FUNC_DEF_PATTERN doesn't match 'static' functions, so correctly-static
    helpers are skipped — only a non-static function is taken as the C0 entry.)"""
    for m in _FUNC_DEF_PATTERN.finditer(leading):
        if leading[m.end():].lstrip().startswith("{"):
            return m.group(1)
    return None


def _warn_sloppy_helpers(leading: str, entry: str) -> None:
    """Warn for any non-static function beyond the entry in the C0 region —
    helpers should be 'static' so the C0 entry stays unambiguous."""
    for m in _FUNC_DEF_PATTERN.finditer(leading):
        name = m.group(1)
        if name != entry and leading[m.end():].lstrip().startswith("{"):
            warn(f"'{name}()' in the C0 region is not declared 'static'. "
                 f"'{entry}()' is taken as the C0 entry; mark helpers 'static' "
                 f"(a non-static helper can be mis-detected as the entry).")


def _excise_function(text: str, func_name: str) -> tuple[str, str]:
    """Return (function_definition, text_with_that_definition_removed), via
    brace matching. Pulls the C0 entry out of the shared C2 preamble."""
    m = make_func_pattern(func_name).search(text)
    if not m:
        die(f"Could not locate '{func_name}()' to excise from the C0 region.")
    brace_start = text.find("{", m.end())
    if brace_start == -1:
        die(f"Could not find opening brace of '{func_name}()'.")
    depth, brace_end = 0, brace_start
    for i, ch in enumerate(text[brace_start:], brace_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                brace_end = i
                break
    else:
        die(f"Could not find closing brace of '{func_name}()'.")
    return text[m.start():brace_end + 1], text[:m.start()] + text[brace_end + 1:]


def split_codes(source: str, is_asm: bool) -> list[dict]:
    """Split a source file into tagged sections in source order.

    Leading region (before the first // Address:) -> a C0 section: for C when it
    holds a non-static function, for ASM when it holds emitted instructions.
    Each // Address: -> next-// Address: slice -> a C2 section.

    Legacy files with no leading function/code yield only C2 sections, exactly
    as before — that's the backward-compat path, not a separate branch.
    """
    matches = list(ADDRESS_PATTERN.finditer(source))
    leading = source[:matches[0].start()] if matches else source

    sections: list[dict] = []

    # ---- C0 (leading region) ----
    if is_asm:
        if INSTRUCTION_PATTERN.search(leading):
            warn("// Instruction: is ignored in ASM files.")
        if _asm_has_content(leading):
            sections.append({"type": "c0", "address": None,
                             "instruction": None, "source": leading})
        preamble = ""   # ASM: leading region is not shared into C2 sections
    else:
        if INSTRUCTION_PATTERN.search(leading):
            warn("// Instruction: before the first // Address: is ignored "
                 "(it is only honored inside a C2 section of a C file).")
        entry = _leading_c_entry(leading)
        if entry:
            _warn_sloppy_helpers(leading, entry)
            # Excise the C0 entry so it isn't compiled into every C2 unit /
            # forward-declared static against its non-static definition.
            _, preamble = _excise_function(leading, entry)
            sections.append({"type": "c0", "address": None,
                             "instruction": None, "source": leading,
                             "entry": entry})
        else:
            preamble = leading   # legacy: shared includes/defines/static helpers

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

        instr_m = INSTRUCTION_PATTERN.search(section_text)
        if is_asm:
            if instr_m:
                warn(f"// Instruction: is ignored in ASM files "
                     f"(section at {addr:#010x}).")
            sections.append({"type": "c2", "address": addr,
                             "instruction": None, "source": section_text,
                             "state": sec_state})
        else:
            sections.append({"type": "c2", "address": addr,
                             "instruction": instr_m.group(1).strip() if instr_m else None,
                             "source": preamble + section_text,
                             "section_text": section_text,
                             "state": sec_state})

    return sections


# ==============================================================================
# ENTRY POINT
# ==============================================================================
def main():
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
    with open(c_path, "r") as f:
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

    author = parse_author(source)
    notes  = parse_notes(source, is_asm)
    state  = parse_file_state(source)
    cond_addr, cond_value = state if state else (None, None)

    sections = split_codes(source, is_asm)
    if not sections:
        die("No code sections found — need a C0 region (code before the first "
            "// Address:) or at least one // Address: comment.")

    n_c0 = sum(1 for s in sections if s["type"] == "c0")
    n_c2 = sum(1 for s in sections if s["type"] == "c2")

    print(f"[INFO] Mode           : {'ASM' if is_asm else 'C'}")
    print(f"[INFO] Name           : {name}")
    print(f"[INFO] Sections       : {len(sections)}  ({n_c0} C0, {n_c2} C2)")
    if author:
        print(f"[INFO] Author         : {author}")
    if state:
        print(f"[INFO] State          : file-level conditional wrapper "
              f"{state[0]:#010x} {state[1]:#010x}")

    check_tools(need_gcc=not is_asm)

    tmpdir = tempfile.mkdtemp(prefix="c2gecko_")
    try:
        all_code_lines: list[str] = []
        for i, section in enumerate(sections):
            loc = (f"{section['address']:#010x}" if section["address"] is not None
                   else "leading region")
            extra = (f"  // Instruction: {section['instruction']}"
                     if section.get("instruction") else "")
            sec_state = section.get("state")
            if sec_state:
                extra += f"  // State: {sec_state[0]:#010x} {sec_state[1]:#010x}"
            print(f"[INFO] Section {i + 1}/{len(sections)} : "
                  f"{section['type'].upper():3} @ {loc}{extra}")

            lines = _build_section(section, i, tmpdir, is_asm, debug)
            if sec_state:
                # gate this injection alone; nests fine inside a file-level State
                s_addr, s_val = sec_state
                lines = ([f"{s_addr:08X} {s_val:08X}"] + lines
                         + ["E2000001 00000000"])
            all_code_lines.extend(lines)

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