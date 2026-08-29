# CGecko

A modding package designed to maximize convenience and efficiency for GameCube modders.

This repo contains:
- `Common.s` and `Common.h` — macros to assist in writing mods efficiently
- `cgecko.py` — build script that converts `.asm`/`.c`/`.ini` files into Gecko codes
- Dolphin integration for automatic code deployment and game launch

**[Full documentation →](../../wiki)**

### Quick Tips
- To pull CGecko submodule updates:
```bash
git submodule update --remote CGecko
```
- See [ProjectRio-ASM](https://github.com/ProjectRio/ProjectRio-ASM) for examples

---

## Requirements

- Python 3.10+
- [devkitPPC](https://devkitpro.org/) — see [installation instructions](../../wiki/Home#installing-devkitpro)

---

## Usage

```
python3 cgecko.py [input.c|input.asm] [-d]
```

Copy `config.template.json` to `config.json` and fill in your paths before first use. See [Configuration](../../wiki/Configuration) for details.

---

## Writing C Mods

There are exactly two macros — **`CGECKO`** for C and **`ASM`** for raw assembly. Whether a code is injected at an address or runs every frame is decided by one thing: pass `.address` and it's an injection (Gecko C2/C3); omit it and it runs once per frame (Gecko C0).

`Common.h` is force-included into every C mod, so both macros and all the helpers are always in scope; an explicit `#include <Common.h>` is optional (and harmless).

```c
CGECKO(give_points, .address = 0x80234564, .state = MSSB_GAME,
                    .instruction = "lwz r3, 0(r4)");
void give_points(void) {
    READ_GAME_REG(int, base, 3);        // r3 at the injection point
    static int total = 0;               // persists across frames
    total += base;
    VAR_ADDRESS(int, 0x8080AABC) = total;
}

CGECKO(each_frame, .state = MSSB_GAME);   // no .address -> runs once per frame
void each_frame(void) { /* ... */ }
```

The body of a `CGECKO` code is an ordinary `void name(void)` function; cgecko wraps it — saving the game's registers, calling your function, then restoring them and returning to the game — so inside it you just write normal C. Locals, `static` state, and helper calls all work.

Any number of codes can live in one file — each becomes its own Gecko code under the file's `$name`. Metadata is emitted into the object, so codes can be split across `#include`d files.

| Field | Meaning | Default |
|---|---|---|
| `.address` | injection address | omitted → runs once per frame |
| `.state` | `MSSB_ALWAYS` / `MSSB_BOOT` / `MSSB_MENU` / `MSSB_GAME` | `MSSB_ALWAYS` |
| `.instruction` | PPC asm re-run just before returning to the game | none |
| `.notes` | what the code does, in prose | none |

### Describing a code: `.notes`

A code's description can be a `// *` comment or the `.notes` field. Both end up
under the code in the ini; only `.notes` survives to runtime.

```c
CGECKO(CPUAlwaysSprints, .state = MSSB_GAME,
       .notes = "CPU runners and the selected CPU fielder\n"
                "always sprint. Human players are\n"
                "unaffected.");
```

Embedded newlines become separate lines wherever the notes are shown. `// *`
comments still work and are still emitted, so nothing has to change; a file may
use either, or both.

The reason to prefer `.notes` is that a comment does not survive the
preprocessor, so an `#include`d mod's blurb is invisible to the code that
included it. `.notes` is data in the record, which means a **DOL-baked** build
can hand it back at runtime:

```c
const char* CGecko_NotesForOption(word option_addr);  // declared in Common.h
```

Keyed on `CGECKO_OPTION_ADDR`, which defaults to the code's `CGECKO_GATE_ADDR` —
because that is the key a mod-options menu already holds: a row owns the toggle
word, so it can ask for the notes of whatever mod that word switches. Returns `0`
when nothing with that option declared `.notes`; a code belonging to no option is
skipped, since `0` is not a key.

Set `CGECKO_OPTION_ADDR` on its own for a mod that reads its option itself
instead of being gate-wrapped — a mod that patches game code has to keep running
while the option is OFF so it can undo itself, which a gate-wrapped code can
never do, but it still belongs to that option:

```c
#undef  CGECKO_OPTION_ADDR
#define CGECKO_OPTION_ADDR MODOPT_ADDR(MODOPT_DUPLICATES)
#include "Gecko Codes/Menu/Duplicate Characters.c"
#undef  CGECKO_OPTION_ADDR
#define CGECKO_OPTION_ADDR CGECKO_GATE_ADDR
```

This only exists in DOL-baked builds (`cgecko_iso.py`), which link the whole pack
as one translation unit. The gecko path links each hook separately, so a mod
included beside yours is not in your translation unit and its notes cannot be
reached — there, `.notes` goes to the ini and stops. Calling
`CGecko_NotesForOption` from a code built as a gecko code is a link error.

### ASM codes

A `CGECKO` code is wrapped (registers saved/restored, called), so it reaches the game's registers through `READ_GAME_REG`. When you need code that runs **verbatim on the game's live registers** — like a hand-written `.asm` injection, but bundled into the same `$name` as your C codes — use `ASM`. It gets no wrapper, no PIC, and no `blr` rewriting; you manage registers and returns yourself, and it can't use C `static` data (use absolute addresses).

The body is the **second argument** — adjacent string literals with no commas between them — and any metadata follows it:

```c
ASM(bump_r3,
    "addi 3, 3, 1 \n"        // r3 is the game's LIVE r3
    "stw  3, 0(4) \n",
    .address = 0x80100000, .state = MSSB_GAME);
```

An injected `ASM` code runs inline and falls through to the handler's branch-back (don't add a trailing `blr`). A per-frame `ASM` code — one with no `.address` — must end in `blr` to return to the codehandler.

See **[`example.c`](example.c)** for a complete tour of every feature, and `Common.h` for memory / register / game-function helpers. `// Author:` and `// *` note comments still work at the top of a file — they become the code's author tag and notes (see `.notes` above for the alternative).

> `READ_GAME_REG` binds a hidden `r30` register, so use it once per function scope; to read several registers put each in its own `{ }` block. `WRITE_GAME_REG` is self-scoped and can be used freely.
