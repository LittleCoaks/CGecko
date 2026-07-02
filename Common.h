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
 * VAR_ADDRESS(type, count, addr)           — 1D array
 * VAR_ADDRESS(type, rows, cols, addr)      — 2D array
 *
 * Examples:
 *   VAR_ADDRESS(int,  0x80123456) = 10;
 *   int x = VAR_ADDRESS(int, 0x80123456);
 *   VAR_ADDRESS(byte, 4, 0x80AABBCC)[2] = 0xFF;
 */
#define GET_MACRO(_1,_2,_3,_4,NAME,...) NAME
#define VAR_ADDRESS(...) GET_MACRO(__VA_ARGS__, arrayValue2D_atAddr, arrayValue_atAddr, singleValue_atAddr)(__VA_ARGS__)

#define singleValue_atAddr(type, addr)                    (*(type *)(addr))
#define arrayValue_atAddr(type, count, addr)              (*(type (*)[count])(addr))
#define arrayValue2D_atAddr(type, rows, cols, addr)       (*(type (*)[rows][cols])(addr))

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

#endif /* COMMON_H */