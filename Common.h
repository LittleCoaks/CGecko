/*
 * Common.h
 * Hook declarations, helper macros, and type definitions for C gecko code
 * injection on GameCube. cgecko force-includes this header into every C mod,
 * so an explicit #include is optional (and harmless).
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

/* ── Code declarations ───────────────────────────────────────────────────────
 *
 * Two macros — one per language:
 *
 *     CGECKO(name, ...)          declares a C code; you write `void name(void)`
 *     ASM(name, "body", ...)     declares an asm code; the body IS the string
 *
 * There is no separate "hook" vs "per-frame" macro. Pass `.address` and the code
 * is injected at that game address (Gecko C2/C3); omit `.address` and it runs
 * once every frame (Gecko C0). Field order is free, and any omitted field is
 * zero — i.e. MSSB_ALWAYS, no instruction.
 *
 *     Fields    .address       injection site; omit for a per-frame code
 *               .state         MSSB_ALWAYS / MSSB_BOOT / MSSB_MENU / MSSB_GAME
 *               .instruction   PPC asm re-run just before returning to the game
 *
 * cgecko reads these records straight out of the compiled object's
 * `.cgecko_hooks` section, so the metadata survives #include and travels with
 * the code it belongs to — unlike a comment, which the preprocessor discards.
 */

/* Scene gates. cgecko maps these to Project Rio scene-id compares; a code runs
 * only when the game is in the named state. MSSB_ALWAYS runs unconditionally. */
#define MSSB_ALWAYS 0
#define MSSB_BOOT   1
#define MSSB_MENU   2
#define MSSB_GAME   3

/* Internal record flags — set by the macros, never by hand. */
#define CGECKO_ASM_FLAG  (1u << 0)   /* body is verbatim asm, no wrapper */

/* Max length of an .instruction string (inline in the record, so cgecko needs
 * no relocation to read it). PPC mnemonics are short; 64 is ample. */
#define CGECKO_INSTR_MAX 64

struct CGeckoHook {
    word   address;                        /* injection site; 0 = per-frame (C0) */
    word   state;                          /* MSSB_ALWAYS / MSSB_MENU / ...       */
    word   flags;                          /* CGECKO_ASM_FLAG | ...               */
    char   instruction[CGECKO_INSTR_MAX];  /* asm re-run after the body, or ""    */
    void (*fn)(void);                      /* the code body                       */
    word   gate_addr;                      /* runtime on/off switch; 0 = always on */
    word   gate_value;                     /* the u32 that switch must hold to run  */
};

/* -- CGECKO_GATE_ADDR / _VALUE: gate whole codes at INCLUDE time -----------
 * Every record picks up whatever these hold WHERE THE CGECKO()/ASM() LINE IS
 * EXPANDED, so one file can wrap somebody else's mod without editing it:
 *
 *     #undef  CGECKO_GATE_ADDR
 *     #define CGECKO_GATE_ADDR 0x802EB010      // a word you toggle at runtime
 *     #include "Gecko Codes/Match/Some Mod.c"  // ALL its hooks now gated
 *     #undef  CGECKO_GATE_ADDR
 *     #define CGECKO_GATE_ADDR 0
 *
 * cgecko turns a non-zero gate into a gecko `20` (32-bit if-equal) conditional
 * around the WHOLE code, which is stronger than an early return in the body:
 * a gated-off C2 is never applied, so its .instruction never runs either and
 * the game is left genuinely stock. That is the only way to switch off a
 * REPLACE hook (.instruction = "blr"), and it works for raw ASM() codes too,
 * which have no C body to return from. */
#ifndef CGECKO_GATE_ADDR
#define CGECKO_GATE_ADDR 0
#endif
#ifndef CGECKO_GATE_VALUE
#define CGECKO_GATE_VALUE 1
#endif

#define _CGECKO_RECORD(fn_, ...)                                                   \
    void fn_(void);                                                                \
    static const struct CGeckoHook __attribute__((section(".cgecko_hooks"), used))  \
        _cgeckohook_##fn_ = { .fn = (void (*)(void))(fn_),                          \
                              .gate_addr  = CGECKO_GATE_ADDR,                       \
                              .gate_value = CGECKO_GATE_VALUE, ##__VA_ARGS__ }

/* ── CGECKO: a C code ────────────────────────────────────────────────────────
 * The body is an ordinary `void name(void)` function. cgecko wraps it: it saves
 * the game's registers, establishes the PIC base (r31), CALLS your function,
 * then restores the game's registers and hands control back. Write normal C —
 * locals, helper calls, and READ_GAME_REG/WRITE_GAME_REG (below) all work.
 *
 *     CGECKO(on_hit, .address = 0x80234567, .state = MSSB_GAME,
 *                    .instruction = "lwz r3, 0(r4)");
 *     void on_hit(void) { ... }
 *
 *     CGECKO(each_frame, .state = MSSB_GAME);   // no .address -> runs per frame
 *     void each_frame(void) { ... }
 *
 * The CGECKO line may sit before or after the function (the macro forward-
 * declares it). */
#define CGECKO(fn_, ...) _CGECKO_RECORD(fn_, ##__VA_ARGS__)

/* ── ASM: a raw, unwrapped code ──────────────────────────────────────────────
 * The body runs VERBATIM — no BACKUP/RESTORE wrapper and no call, exactly like a
 * hand-written .asm injection. r3–r31 are the game's LIVE registers, and you
 * manage registers and returns yourself. An ASM code cannot use C `static` data
 * (there is no PIC base) — use absolute / claimed addresses. (`naked` C
 * functions are not an option: PPC GCC ignores `naked`, so the body is emitted
 * verbatim through top-level asm.)
 *
 * The body is the SECOND argument — adjacent string literals, no commas between
 * them — and any metadata follows it:
 *
 *     ASM(bump,
 *         "addi 3, 3, 1 \n"                  // r3 is the game's live r3 here
 *         "stw  3, 0(4) \n",
 *         .address = 0x80100000, .state = MSSB_GAME);
 *
 * An injected (C2) ASM code runs inline and then falls through to the handler's
 * branch-back, so end by falling through — no trailing blr. A per-frame (C0) ASM
 * code — one with no .address — must end in `blr` to return to the codehandler. */
#define ASM(fn_, body_, ...)                                  \
    _CGECKO_RECORD(fn_, .flags = CGECKO_ASM_FLAG, ##__VA_ARGS__); \
    asm(".section .text." #fn_ ",\"ax\",@progbits\n"          \
        ".globl " #fn_ "\n"                                   \
        #fn_ ":\n"                                            \
        body_)

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
 *   BACKUP saves r3–r31 into the hook's stack frame; this binds 'name' to that
 *   saved slot, reached via r30 (the frame base).
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
 * Note: r30 is the frame base and is preserved across calls, so READ_GAME_REG /
 *   WRITE_GAME_REG work both in the hook and in any helper it calls. (READ_REG
 *   for r0–r2 does not — those live registers are only meaningful in the hook.)
 * Note: VSCode may show squiggles on register variable syntax — ignore them.
 */
#define READ_GAME_REG(type, name, num)                           \
    register unsigned int _sp __asm__("r30");                     \
    type name = *(volatile type*)(                               \
        _sp + 0x8 + (((num) - 3) << 2)                           \
    )

/* Writes through r30 (the backup frame base) to the saved-register slot, scoped
 * in a do/while so repeated writes are fine. RESTORE (lmw r3) loads the slots
 * back into the real registers before control returns to the game. */
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
 *   Works in the hook and in helpers it calls (r30 is preserved across calls).
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

static inline void PatchInstruction(word addr, word value)
{
    volatile word* site = (volatile word*)addr;
    if (*site == value)
        return;
    *site = value;
    asm volatile("dcbst 0,%0\n\tsync\n\ticbi 0,%0\n\tisync" :: "r"(site) : "memory");
}
// Patch one instruction, only when the expected original is there
// (idempotent: after patching, the site no longer matches, and a
// freshly reloaded REL matches again). The cache ops keep it
// correct on real hardware; Dolphin doesn't need them.
static inline void PatchInstruction_Conditional(word addr, word original, word replacement)
{
    volatile word* site = (volatile word*)addr;
    if (*site != original)
        return;
    *site = replacement;
    asm volatile("dcbst 0,%0\n\tsync\n\ticbi 0,%0\n\tisync" :: "r"(site) : "memory");
}

#endif /* COMMON_H */
