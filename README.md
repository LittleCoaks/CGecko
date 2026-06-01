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
