/*
 * Common.h
 * Helper macros and type definitions for C gecko code injection on GameCube.
 * See README.md for full documentation. For ASM macros, see Common.s.
 */

#ifndef COMMON_H
#define COMMON_H

/* ── Type aliases ────────────────────────────────────────────────────────── */

/* bool/true/false are built-in keywords in C++ and in the C23 era. GCC makes
 * them keywords in -std=c2x (draft, __STDC_VERSION__ == 202000L) AND -std=c23
 * (final, 202311L) — and GCC 15 defaults to gnu23 — so test for >= 202000L, not
 * just the final value. Only define them ourselves on C17 and earlier; the
 * built-ins are used otherwise. The __cplusplus half also avoids IDE false
 * positives when an editor parses this header as C++. */
#if !defined(__cplusplus) && (!defined(__STDC_VERSION__) || __STDC_VERSION__ < 202000L)
typedef unsigned char bool;
#define false 0
#define true  1
#endif

typedef unsigned char  byte;
typedef unsigned short halfword;
typedef unsigned int   word;

/* ── Memory access ───────────────────────────────────────────────────────── */
/*
 * VAR_ADDRESS(type, addr)                  — single value
 * ARRAY_1D_ADDRESS(type, count, addr)           — 1D array
 * ARRAY_2D_ADDRESS(type, rows, cols, addr)      — 2D array
 *
 * Examples:
 *   VAR_ADDRESS(int,  0x80123456) = 10;
 *   int x = VAR_ADDRESS(int, 0x80123456);
 *   VAR_ADDRESS(byte, 4, 0x80AABBCC)[2] = 0xFF;
 */
#define VAR_ADDRESS(type, addr) (*(type *)(addr))
#define ARRAY_1D_ADDRESS(type, n, addr) (*(type (*)[n])(addr))
#define ARRAY_2D_ADDRESS(type, r, c, addr) (*(type (*)[r][c])(addr))
#define ARRAY_3D_ADDRESS(type, x, y, z, addr) (*(type (*)[x][y][z])(addr))
#define ARRAY_4D_ADDRESS(type, a, b, c, d, addr) (*(type (*)[a][b][c][d])(addr))
#define ARRAY_5D_ADDRESS(type, a, b, c, d, e, addr) (*(type (*)[a][b][c][d][e])(addr))

/* ── Function calls ──────────────────────────────────────────────────────── */
/*
 * Call a game function by its memory address.
 * Expands to an inline cast — no declaration, no scope issues, works anywhere.
 *
 * Examples:
 *   FUNCTION_ADDRESS(void, 0x800c836C, int, int, int, int)(soundID, 127, 0x3f, 0x0);
 *   FUNCTION_ADDRESS(int,  0x80123456, float)(1.5f);
 *
 * For repeated calls, assign to a local function pointer:
 *   void (*PlaySound)(int, int, int, int) = (void(*)(int,int,int,int))(0x800c836C);
 *   PlaySound(soundID, 127, 0x3f, 0x0);
 *   PlaySound(soundID2, 64, 0x3f, 0x0);
 */
#define FUNCTION_ADDRESS(returnType, addr, ...) ((returnType (*)(__VA_ARGS__))(addr))

/* ── Register access ────────────────────────────────────────────────────────── */
/*
 * READ_GAME_REG(type, name, reg_num)
 *   Read the game's GPR value at the injection point. r3–r31 only.
 *   BACKUP saves r3–r31 to the stack but leaves the live registers unchanged,
 *   so the live value equals the saved value. Binds 'name' directly to r<reg_num>.
 *
 * WRITE_GAME_REG(reg_num, val)
 *   Write a value to the BACKUP stack slot for a GPR. r3–r31 only.
 *   RESTORE (lmw r3) reloads from the stack, so the game sees the new value
 *   in that register after the gecko code returns.
 *
 * READ_REG(type, name, num)
 *   Bind a variable to a live hardware register. Use only for r0–r2,
 *   which are not in the BACKUP frame. Use cautiously.
 *
 * Examples:
 *   READ_GAME_REG(int, score, 3);    // score = r3 at injection point
 *   WRITE_GAME_REG(3, score + 1);    // game sees r3+1 after gecko code returns
 *   READ_REG(int, raw, 0);           // r0 (scratch, not in BACKUP frame)
 *
 * Note: these macros are only valid in the entry function, not in helper functions.
 * Note: VSCode may show squiggles on register variable syntax — ignore them.
 */
#define READ_GAME_REG(type, name, num)                           \
    register unsigned int _sp __asm__("r30");                     \
    type name = *(volatile type*)(                               \
        _sp + 0x8 + (((num) - 3) << 2)                           \
    )

/* Local register variable reads r1 (backup frame base) without an asm template,
 * avoiding the -mregnames issue entirely. GCC optimizes to a direct stw with
 * r1 as the base register. Do not declare this at file scope — that reserves
 * r1 globally and breaks normal helper functions. */
#define WRITE_GAME_REG(num, val) \
    do { register unsigned int _sp __asm__("r30"); \
         *(volatile unsigned int*)(_sp + 0x8 + (((num) - 3) << 2)) = (unsigned int)(val); \
    } while(0)

#define READ_REG(type, name, num) register type name __asm__(#num)

/* ── Utilities ───────────────────────────────────────────────────────────── */

#define LEN(a)          (sizeof(a) / sizeof(*a))    // number of elements in array
#define SQUARE(a)       ((a) * (a))
#define OFFSET_OF(st, m) ((size_t)&(((st *)0)->m))   // byte offset of struct member

/* ── Tips ────────────────────────────────────────────────────────────────── */
/*
 * INJECTION SITES:
 *   Choose safe, idle instructions like li or lis as injection points.
 *   Avoid injecting mid-calculation or mid-loop.
 *
 * REGISTER VARIABLES:
 *   Use READ_GAME_REG to read the game's register state at the injection point:
 *     READ_GAME_REG(int, score, 30);   // reads saved r30
 *     WRITE_GAME_REG(30, score + 1);  // game sees r30+1 after gecko code returns
 *   Only valid in the entry function, not in helpers.
 */

 /* ── Float helpers ────────────────────────────────────────────────────────── */
// cpp2gecko implementation
// _scratch lets the compiler choose an available reg
#define LOAD_FLOAT(value, result_reg) do { \
    union { float f; unsigned int i; } _bits = {.f = (value)}; \
    unsigned int _scratch;  \
    asm("lis %1, %2\n\t" \
        "ori %1, %1, %3\n\t" \
        "stw %1, -8(1)\n\t" \
        "lfs %0, -8(1)" \
        : "=f"(result_reg), "=&r"(_scratch) \
        : "n"((short)(_bits.i >> 16)), "n"((unsigned short)(_bits.i & 0xFFFF)) \
        : "memory"); \
} while(0)

#define FP(x) ({ \
    register float _fp_tmp; \
    LOAD_FLOAT((x), _fp_tmp); \
    _fp_tmp; \
})

// Patch one instruction, only when the expected original is there
// (idempotent: after patching, the site no longer matches, and a
// freshly reloaded REL matches again). The cache ops keep it
// correct on real hardware; Dolphin doesn't need them.
static inline __attribute__((always_inline)) void PatchInstruction(word addr, word original, word replacement)
{
    volatile word* site = (volatile word*)addr;
    if (*site != original)
        return;
    *site = replacement;
    asm volatile("dcbst 0,%0\n\tsync\n\ticbi 0,%0\n\tisync" :: "r"(site) : "memory");
}

#endif /* COMMON_H */