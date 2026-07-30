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

See **[`example.c`](example.c)** for a complete tour of every feature, and `Common.h` for memory / register / game-function helpers. `// Author:` and `// *` note comments still work at the top of a file — they become the code's author tag and notes.

> `READ_GAME_REG` binds a hidden `r30` register, so use it once per function scope; to read several registers put each in its own `{ }` block. `WRITE_GAME_REG` is self-scoped and can be used freely.
