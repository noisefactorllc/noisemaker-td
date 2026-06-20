# Reference Spec 01 — DSL Frontend (Polymorphic DSL: Lexer + Parser + AST)

Scope: the front-end of the Noisemaker "Polymorphic" live-coding DSL. Source of truth:
`shaders/src/lang/{index,lexer,parser,constants,diagnostics,ops,effectAliases,paramAliases,enumPaths,std_enums,enums,error-formatter}.js`.

This document describes ONLY tokenization and parsing into an AST ("plans"). The
validator (`validate()`), unparser, and transform passes are downstream and out of scope here,
but the AST shape this front-end emits is fully specified so they can be re-implemented against it.

Pipeline entry point (`index.js`):

```js
export function compile(src) {
    const tokens = lex(src)      // lexer.js
    const ast = parse(tokens)    // parser.js
    return validate(ast)         // validator.js (downstream, returns {plans, diagnostics, render})
}
```

`parse(tokens)` returns a `Program` AST node (see §6). `compile()` wraps it with validation; a
re-implementation that only needs the parse tree calls `lex` then `parse`.

---

## 1. Lexer (`lexer.js`)

### 1.1 Token object shape

Every token is `{ type, lexeme, line, col }`.
- `type`: string token kind (uppercase, see §1.4).
- `lexeme`: the matched source substring (for STRING/FUNC it is the *content* without delimiters; see below).
- `line`: 1-based line number at token start.
- `col`: 1-based column number at token start.

The stream always ends with one `{type:'EOF', lexeme:'', line, col}`.

### 1.2 Position tracking semantics (PARITY HAZARD)

- `line` starts at 1, `col` starts at 1.
- Space, tab, `\r` advance `col` by 1 each and are skipped (no token).
- `\n` increments `line` and resets `col = 1`.
- Tokens record `startLine`/`startCol` captured *before* consuming.
- `col` advances by `j - i` (number of chars consumed) for multi-char tokens.
- Column counting is **per-character (code unit) based**, not display-width based. Tabs count as 1 column. A faithful port must count UTF-16 code units exactly as JS `String` indexing does, because error messages embed `line`/`col` and `error-formatter.js` (§9) parses them back out and points at `col-1` spaces. Any divergence in column math changes user-visible error carets but not the AST.

### 1.3 Reserved keywords (`constants.js`, `RESERVED_KEYWORDS`, frozen)

Identifier-shaped lexemes matching a key here emit the mapped token type instead of `IDENT`:

| keyword | token type |
|---|---|
| `let` | `LET` |
| `render` | `RENDER` |
| `write` | `WRITE` |
| `write3d` | `WRITE3D` |
| `true` | `TRUE` |
| `false` | `FALSE` |
| `if` | `IF` |
| `elif` | `ELIF` |
| `else` | `ELSE` |
| `break` | `BREAK` |
| `continue` | `CONTINUE` |
| `return` | `RETURN` |
| `search` | `SEARCH` |
| `subchain` | `SUBCHAIN` |

### 1.4 Tokenization rules, in exact priority order

The lexer is a single `while (i < src.length)` loop. Each iteration tests rules **in this order**;
first match wins, then `continue`. Ordering is load-bearing (e.g. triple-quote before single-quote,
surface refs before identifiers).

1. **Whitespace**: ` `, `\t`, `\r` → skip, `col++`. `\n` → `line++`, `col=1`.
2. **Line comment** `//…`: scan to `\n` (exclusive). Emit `COMMENT` with full lexeme including `//`. `col += len`.
3. **Block comment** `/*…*/`: scan to `*/`. Tracks `endLine`/`endCol` across newlines. Unterminated → `throw SyntaxError("Unterminated comment at line L col C")`. Emit `COMMENT` with full lexeme including delimiters. Final `col = endCol + 2`.
4. **Output/Source ref**: char is `o` or `s` AND `src[i+1]` is a digit. Greedily consume following digits. `o`→`OUTPUT_REF`, `s`→`SOURCE_REF`. Lexeme e.g. `o0`, `s3`, `o12`. **NOTE**: any number of digits is consumed; validation of range (0..7) is downstream.
5. **`vol` ref**: `v o l` + digit at `i..i+3` → `VOL_REF`, consume digits from `i+3`.
6. **`geo` ref**: `g e o` + digit → `GEO_REF`.
7. **`xyz` ref**: `x y z` + digit → `XYZ_REF` (agent position surface).
8. **`vel` ref**: `v e l` + digit → `VEL_REF` (agent velocity surface). NOTE: rule 5 (`vol`) is tested before rule 8 (`vel`); `v` disambiguated by 3rd char.
9. **`rgba` ref**: `r g b a` + digit at `i..i+4` → `RGBA_REF`.
10. **`mesh` ref**: `m e s h` + digit at `i..i+4` → `MESH_REF`.
11. **Hex color** `#`: consume `[0-9a-fA-F]`. Emit `HEX` ONLY if total length (including `#`) is exactly 4, 7, or 9 (i.e. 3/6/8 hex digits). Otherwise the `#` rule does *not* match and falls through (which then hits the catch-all `throw` at the bottom, since `#` matches no later rule).
12. **Arrow function** `()` `=>`: char `(` and `src[i+1]===')'`. Skip spaces/tabs, require `=` `>`. Then skip spaces/tabs and capture the body expression by scanning with paren depth tracking: increment on `(`, decrement on `)` (break if depth 0 at `)`), and at depth 0 break on `,` `;` `\n` `}`. Emit `FUNC` with lexeme = trimmed expression source (no `()=>`). `col += j-i`.
13. **Leading-dot number** `.D`: `.` followed by digit. Consume digits → `NUMBER` with lexeme like `.5`.
14. **Single-char punctuation** (each emits its type, `i++`, `col++`):
    `.`→`DOT`, `(`→`LPAREN`, `)`→`RPAREN`, `{`→`LBRACE`, `}`→`RBRACE`, `[`→`LBRACKET`, `]`→`RBRACKET`, `,`→`COMMA`, `:`→`COLON`, `=`→`EQUAL`, `;`→`SEMICOLON`, `+`→`PLUS`, `-`→`MINUS`, `*`→`STAR`, `/`→`SLASH`.
    (`.` is reached only after the `.D` number rule, so `.` always means member/dot here.)
15. **Triple-quoted string** `"""…"""`: scanned before single quotes. Tracks newlines (`line++`). Unterminated → `throw SyntaxError("Unterminated triple-quoted string …")`. Emit `STRING` with content between the triple quotes (delimiters stripped). Multi-line col fixup: `col = lastLine.length + 4`.
16. **Single/double quoted string** `"…"` or `'…'`: consume to matching quote; backslash escapes consume 2 chars (`\\` + next). A `\n` before close or EOF → `throw SyntaxError("Unterminated string literal …")`. Emit `STRING` with content (quotes stripped). **The escape sequences are NOT decoded** — `\n` stays as the two characters backslash+n in the lexeme; the parser/validator must decide. (Triple-quoted strings do NOT process escapes at all.)
17. **Number** `D…`: digits, optionally `.` + digits (requires a digit after `.`). Emit `NUMBER`. Lexeme like `12`, `3.5`. No exponent, no hex, no leading `+/-` (sign handled by parser unary).
18. **Identifier/keyword**: `[A-Za-z_]` then `[A-Za-z0-9_]*`. If lexeme in `RESERVED_KEYWORDS` → keyword token; else `IDENT`.
19. **Anything else** → `throw SyntaxError("Unexpected character 'C' at line L col C")`.

PARITY HAZARDS (lexer):
- `isLetter` is ASCII-only (`a-z`,`A-Z`); `isDigit` is `0-9`. No Unicode letters. Identifiers cannot contain non-ASCII.
- Surface-prefix rules (4–10) rely on bare character compares at fixed offsets, e.g. `xyz5` lexes as `XYZ_REF` but `xyzzy` (no digit at index 3) falls through to the identifier rule and becomes `IDENT "xyzzy"`. A C# port must replicate "prefix-letters + immediate digit" exactly, including that `volume`/`velocity`/`geometry`/`meshes`/`rgbaX` only tokenize specially when a digit immediately follows the prefix.
- HEX lengths are gated to {3,6,8} hex digits; 4 or 5 digit hex falls through to error.

---

## 2. Parser overview (`parser.js`)

`parse(tokens)` is a recursive-descent parser with a single mutable cursor `current` (index into
`tokens`). Helpers:
- `peek()` → `tokens[current]`
- `advance()` → returns `tokens[current++]`
- `expect(type, msg)` → if `peek().type === type` advance & return; else `throw SyntaxError(`${msg} at line L col C`)`.
- `collectComments()` → consume contiguous `COMMENT` tokens, return array of their `lexeme` strings.

Parser-local state:
- `programSearchOrder` — `null` until `search` directive parsed; then array of namespace strings.
- `programNamespace = { imports: [], default: null }` — populated by `search`.

### 2.1 Two token-class sets (exact membership)

`exprStartTokens` (a token may begin an expression):
`PLUS, MINUS, NUMBER, HEX, FUNC, STRING, IDENT, OUTPUT_REF, SOURCE_REF, VOL_REF, GEO_REF, MESH_REF, XYZ_REF, VEL_REF, RGBA_REF, LPAREN, LBRACKET, TRUE, FALSE`.

`memberTokenTypes` (a token may be a segment in a dotted member/enum path):
`IDENT, SOURCE_REF, OUTPUT_REF, VOL_REF, GEO_REF, MESH_REF, XYZ_REF, VEL_REF, RGBA_REF, LET, RENDER, TRUE, FALSE, IF, ELIF, ELSE, BREAK, CONTINUE, RETURN, WRITE, WRITE3D, SUBCHAIN`.
(So keyword tokens and surface refs CAN appear as dotted enum segments, e.g. `disp.source.o1`, `sparky.loop.tri`.)

`namespaceTokenTypes` (valid after `search`/comma, local to `parseProgram`):
`IDENT, RENDER, WRITE, WRITE3D, TRUE, FALSE, IF, ELIF, ELSE, BREAK, CONTINUE, RETURN`.

---

## 3. Grammar actually implemented

```
Program        ::= (Comment)* SearchDirective? Statement* RenderDirective? (Comment)*
SearchDirective::= 'search' NsIdent (',' NsIdent)* ';'*       // REQUIRED somewhere before any stmt
RenderDirective::= 'render' '(' OutputRef ')'                 // at most one
Statement      ::= VarAssign | IfStmt | Break | Continue | Return | ChainStmt
VarAssign      ::= 'let' IDENT '=' Additive
IfStmt         ::= 'if' '(' Additive ')' Block ('elif' '(' Additive ')' Block)* ('else' Block)?
Block          ::= '{' Statement* '}'
ChainStmt      ::= Chain                                      // top-level chain of calls
Chain          ::= Call ('.' (Call | WriteCall | SubchainCall))*
WriteCall      ::= 'write' '(' SurfaceRef ')' | 'write3d' '(' Ref ',' Ref ')'
SubchainCall   ::= 'subchain' '(' SubArgs? ')' '{' ('.' Call)+ '}'
Call           ::= IDENT '(' (ArgList | KwargList)? ')'
ArgList        ::= Additive (',' Additive)* ','?
KwargList      ::= Kwarg (',' Kwarg)* ','?
Kwarg          ::= IDENT ':' Additive
Additive       ::= Multiplicative (('+'|'-') Multiplicative)*
Multiplicative ::= Unary (('*'|'/') Unary)*
Unary          ::= '+' Unary | '-' Unary | Primary
Primary        ::= Number | String | Hex | ArrayLiteral | Func | Boolean
                 | Math.PI | Chain-or-Member-or-Ident | OutputRef | SourceRef
                 | VolRef | GeoRef | XyzRef | VelRef | RgbaRef | MeshRef | '(' Additive ')'
```

NOTE: there is NO comparison/boolean/relational operator grammar. `Additive`/`Multiplicative`
fold *only* numeric literals at parse time (see §4.4). `if`/`elif` conditions are parsed by
`parseAdditive` and so a condition is generally a `Chain`/`Member`/`Ident`/`Number` node, NOT a
boolean expression — runtime/validator interprets truthiness.

### 3.1 `parseProgram` control flow (numbered)

1. `plans=[]`, `vars=[]`, `render=null`, `trailingComments=[]`.
2. Loop while `peek().type !== 'EOF'`:
   a. If `SEMICOLON` → advance, continue (stray semicolons skipped).
   b. `leadingComments = collectComments()`. If now `EOF`: push leadingComments into trailingComments; break.
   c. If `SEARCH`: error if any `plans.length || vars.length || render` already present (`"'search' directive must appear before other statements"`); else `parseSearchDirective()`; continue.
   d. If `RENDER`: `consumeRender()`. Attach `render.leadingComments` if any. Collect trailing comments → `trailingComments`. **break** (render terminates program parse).
   e. Else `stmt = parseStatement()`. Attach `stmt.leadingComments` if any. `appendStatement(stmt)` (routes `VarAssign`→`vars`, everything else→`plans`). Then `while peek==SEMICOLON advance`.
3. `expect('EOF', …)`.
4. If `!programSearchOrder || length===0` → `throw SyntaxError("Missing required 'search' directive. …")`.
5. Build `program` (see §6.1) including `namespace` metadata clone.

`consumeRender()`: errors `"Duplicate render() directive …"` if `render` already set. Then
`parseRenderDirective()`; consumes trailing semicolons.

`parseRenderDirective()`: advance past `render`; `expect LPAREN`; require `peek().type==='OUTPUT_REF'`
(else `throw "Expected output reference in render()"`); build `{type:'OutputRef', name: lexeme}`;
`expect RPAREN`. Returns the OutputRef node directly (so `program.render` is an `OutputRef` node, not a wrapper).

### 3.2 `parseSearchDirective` (numbered)

1. If `programSearchOrder !== null` → `throw "Only one search directive is allowed per program …"`.
2. advance past `search`.
3. First token must be in `namespaceTokenTypes` else `throw "Expected namespace identifier after search …"`. advance.
4. `validateNamespace(token)`: `isValidNamespace(token.lexeme)` (from `runtime/tags.js`) — if false `throw "Invalid namespace 'X' … Valid namespaces: …"`. Push `token.lexeme`.
5. While `peek==COMMA`: advance; next must be in `namespaceTokenTypes` (`throw "Expected namespace identifier after comma …"`); advance; validate; push.
6. `programSearchOrder = namespaces`.
7. `programNamespace.imports = namespaces.map(n => ({name:n, source:'search', explicit:true}))`.
8. `programNamespace.default = {name: namespaces[0], source:'search', explicit:true}`.
9. Consume trailing semicolons.

CROSS-SUBSYSTEM DEP: `isValidNamespace` / `VALID_NAMESPACES` come from `runtime/tags.js`.
`VALID_NAMESPACES = [..._namespaces.keys()]` and is **mutated at runtime** by `registerNamespace()`/
`unregisterNamespace()`. So the set of legal namespaces is *not static* — it is whatever namespaces
have been registered by loaded effect modules. A C# port must build the namespace registry from the
effect manifest before parsing. (Namespace IDs are e.g. effect-pack ids; not enumerated in lang/.)

### 3.3 `parseStatement` (numbered)

1. If `SEARCH` here → `throw "'search' directive is only allowed at the start of the program …"`.
2. If `LET`:
   - advance; `name = expect(IDENT).lexeme`; `expect EQUAL`.
   - If `peek().type` not in `exprStartTokens` → `throw "Expected expression after '=' …"`.
   - `expr = parseAdditive()`. Return `{type:'VarAssign', name, expr}`.
3. switch `peek().type`:
   - `IF`: advance; `expect LPAREN`; `condition = parseAdditive()`; `expect RPAREN`; `then = parseBlock()`. Loop `ELIF`: advance; LPAREN; `ec=parseAdditive()`; RPAREN; `body=parseBlock()`; push `{condition:ec, then:body}` into `elif`. If `ELSE`: advance; `elseBranch=parseBlock()`. Return `{type:'IfStmt', condition, then, elif, else:elseBranch}` (`else` may be `null`).
   - `BREAK`: advance → `{type:'Break'}`.
   - `CONTINUE`: advance → `{type:'Continue'}`.
   - `RETURN`: advance; if next in `exprStartTokens` → `{type:'Return', value: parseAdditive()}` else `{type:'Return'}`.
4. Otherwise: `chain = parseChain()` (default context `'statement'`). Inspect last node:
   - If `chain[last].type==='Write'` → `write = lastNode.surface`.
   - elif `chain[last].type==='Write3D'` → `write3d = {tex3d, geo}`.
   - else both stay `null`.
   Return `{chain, write, write3d}`.
   (Comment: only a *terminal* write counts; mid-chain writes are passthrough nodes in `chain`. A starter chain lacking terminal write yields S006 downstream.)

`parseBlock()`: `expect LBRACE`; loop until `RBRACE` calling `parseStatement()` and consuming trailing semicolons; `expect RBRACE`. Returns array of statements (no wrapper node).

---

## 4. Expressions

### 4.1 `parseChain(context='statement')` (numbered)

1. `firstCall = parseCall()`; `calls=[firstCall]`.
2. Loop:
   a. `savedPos = current`; `leadingComments = collectComments()`.
   b. If `peek().type !== 'DOT'`: restore `current=savedPos` (comments belong to next stmt); break.
   c. advance past `.`; `postDotComments = collectComments()`; `allComments=[...leading,...postDot]`.
   d. `nextType = peek().type`:
      - `WRITE`/`WRITE3D`: if `context==='expression'` → `throw "'.write()' is only allowed in statement context …"`. Else `node = parseWriteCall()`; attach comments; push; continue.
      - `SUBCHAIN`: `node = parseSubchainCall()`; attach comments; push; continue.
      - else: `call = parseCall()`; attach comments; push.
3. Return `calls` (array of nodes — a "chain" is the raw array, not a node, unless wrapped by Primary, see §4.5).

### 4.2 `parseCall()` (numbered)

1. `nameToken = expect(IDENT, …)`.
2. **Inline-namespace rejection**: if `peek==DOT` and `tokens[current+1].type==='IDENT'` and `tokens[current+2].type==='LPAREN'` → `throw "Inline namespace syntax 'X.y()' is not allowed. Use 'search X' …"`. (So `nd.noise()` is illegal; namespaces only via `search`.)
3. `expect LPAREN`. `args=[]`, `kwargs={}`, `keyword=false`.
4. If next is not `RPAREN`:
   - If `peek==IDENT && tokens[current+1].type==='COLON'` → keyword mode: `keyword=true`; `parseKwarg(kwargs)`; while `COMMA`: advance; break if `RPAREN` (trailing comma ok); else require kwarg shape (`IDENT`+`COLON`) or `throw "Cannot mix positional and keyword arguments …"`; `parseKwarg`.
   - Else positional mode: `args.push(parseArg())`; while `COMMA`: advance; break if `RPAREN`; if next looks like kwarg (`IDENT`+`COLON`) → `throw "Cannot mix positional and keyword arguments …"`; else `args.push(parseArg())`.
5. `expect RPAREN`. `call = {type:'Call', name:nameToken.lexeme, args}`; if `keyword` set `call.kwargs = kwargs`.
6. **Special-form transforms** by `nameToken.lexeme` (in this order):
   - `from` → `transformFromInvocation` (§4.6).
   - `osc` → if it looks like a value oscillator (see condition below) → `transformOscInvocation`; else fall through and return as plain `Call` (the `synth.osc` generator effect).
   - `midi` → `transformMidiInvocation`.
   - `audio` → `transformAudioInvocation`.
   - `read` → build `{type:'Read', surface, loc}`; `surface = args[0] || kwargs.tex || kwargs.surface`. If `kwargs._skip` is a `Boolean` node with `value===true`, set `node._skip = true`.
   - `read3d` → `{type:'Read3D', tex3d: args[0]||kwargs.tex3d, geo: args[1]||kwargs.geo || null, loc}`; same `_skip` handling.
   - else return `call`.

`osc` value-oscillator detection (any of):
- `kwargs` has key `type`, OR
- `args[0]` is a `Member` whose `path[0]==='oscKind'`, OR
- bare `osc()` (no args, no kwargs), OR
- all kwargs keys ∈ `{type,min,max,speed,offset,seed}` and there is ≥1 kwarg.

`parseArg()` = `parseAdditive()`.
`parseKwarg(obj)`: `key=expect(IDENT).lexeme`; `expect COLON`; if next not in `exprStartTokens` → `throw "Expected expression after '=' …"` (note message says `'='` though it's `':'`); `obj[key]=parseArg()`.

### 4.3 `hasCallAfterDot(index)` lookahead

Used by Primary `IDENT` branch to detect `foo.bar().…` (member-then-call):
starting at `index+1`, require `DOT`; while DOT, require next token ∈ `memberTokenTypes`, step by 2; after the run, true iff token is `LPAREN`.

### 4.4 Numeric operators are constant-folded AT PARSE TIME (PARITY HAZARD)

`parseAdditive`/`parseMultiplicative`/`parseUnary` immediately reduce to a single `{type:'Number', value}`:

```js
function parseAdditive() {
    let node = parseMultiplicative()
    while (PLUS||MINUS) {
        const op=advance().type; const right=parseMultiplicative()
        const l=toNumber(node); const r=toNumber(right)
        node = {type:'Number', value: op==='PLUS' ? l+r : l-r}
    }
    return node
}
```
- `toNumber(node)` requires `node.type==='Number'` else `throw "Expected number"`. **Therefore arithmetic operands MUST be number literals (or `Math.PI`, or parenthesized number exprs).** `2 * speed` where `speed` is an Ident is a syntax error — you cannot do arithmetic on identifiers/members/refs.
- Multiplicative binds tighter than additive; unary `-`/`+` bind tightest; `()` grouping via Primary `LPAREN`.
- All math is JS IEEE-754 double (`Number`). Division is float (`/`), no integer division. `value` carried into AST is a JS double.
- PARITY: a C# port must perform this folding in `double` and store the double in the AST `Number.value`. Evaluation order is strictly left-to-right within each precedence level. `Math.PI` literal = JS `Math.PI` = `3.141592653589793`.

### 4.5 `parsePrimary()` — node construction per token

- `NUMBER` → `{type:'Number', value: parseFloat(lexeme)}`. (JS `parseFloat`; lexeme has no sign/exponent.)
- `STRING` → `{type:'String', value: lexeme}` (raw, un-unescaped content).
- `HEX` → `{type:'Color', value:[r/255,g/255,b/255,a]}`. See §5.
- `LBRACKET` → ArrayLiteral: consume `[`; comma-separated `parseArg()` elements until `]`; `throw "Expected ']' …"` if missing. Returns `{type:'ArrayLiteral', elements, loc:{line,col}}`. Used as alt vecN input.
- `FUNC` → `{type:'Func', src: lexeme}` (lexeme is the trimmed body from the `()=>` arrow lexer rule).
- `TRUE` → `{type:'Boolean', value:true}`; `FALSE` → `{type:'Boolean', value:false}`.
- `IDENT`:
  1. If lexeme `Math` and `tokens[+1]==DOT` and `tokens[+2]==IDENT 'PI'` → consume 3 → `{type:'Number', value: Math.PI}`.
  2. Else if `tokens[+1]==LPAREN` OR `hasCallAfterDot(current)` → `chain = parseChain('expression')`; return `chain.length===1 ? chain[0] : {type:'Chain', chain}`. (THIS is the only place a `{type:'Chain', chain:[…]}` wrapper node is produced.)
  3. Else dotted member path: advance; `path=[lexeme]`; while `DOT`: peek `next=tokens[+1]`; break if no next; **break if `tokens[+2]==LPAREN`** (that dot begins a call, not a member segment); if `next.type` ∉ `memberTokenTypes` → `throw "Expected identifier after '.' …"`; advance dot + segment; `path.push(next.lexeme)`. Return `{type:'Member', path}` if `path.length>1` else `{type:'Ident', name: path[0]}`.
- `OUTPUT_REF` → `{type:'OutputRef', name:lexeme}`.
- `SOURCE_REF` → `{type:'SourceRef', name:lexeme}`.
- `VOL_REF` → `{type:'VolRef', name:lexeme}`.
- `GEO_REF` → `{type:'GeoRef', name:lexeme}`.
- `XYZ_REF` → `{type:'XyzRef', name:lexeme}`.
- `VEL_REF` → `{type:'VelRef', name:lexeme}`.
- `RGBA_REF` → `{type:'RgbaRef', name:lexeme}`.
- `MESH_REF` → `{type:'MeshRef', name:lexeme}`.
- `LPAREN` → advance; `expr=parseAdditive()`; `expect RPAREN`; return `expr`.
- default → `throw "Unexpected token T at line L col C"`.

### 4.6 `transformFromInvocation(call, nameToken)`

- Reject kwargs (`"'from' does not support named arguments"`).
- Require exactly 2 args (`"'from' requires exactly two arguments (namespace, call)"`).
- arg0 must be `Ident` or `Member` (`"'from' namespace argument must be an identifier"`); `namespaceName = Member? path.join('.') : name`.
- arg1 must be a `Call` (or a `Chain` of length 1 whose head is a `Call`) else `"'from' second argument must be a call expression"`.
- Returns the target call with a `namespace` override object:
```js
{ ...targetCall, args:[...], kwargs?:{...},
  namespace: { name, path:[name], explicit:true, source:'from',
               resolved:name, searchOrder:[name], fromOverride:true } }
```

---

## 5. Color (HEX) parsing — exact arithmetic (PARITY HAZARD)

`parsePrimary` HEX branch (`token.lexeme` includes leading `#`; `hex = lexeme.slice(1)`):

```
len 3 (#RGB):  r=int(c0c0,16) g=int(c1c1,16) b=int(c2c2,16); a=1.0
len 6 (#RRGGBB): r=int(0..2,16) g=int(2..4,16) b=int(4..6,16); a=1.0
len 8 (#RRGGBBAA): r,g,b as 6; a = int(6..8,16)/255
value = [r/255, g/255, b/255, a]
```
- Channel parse is `parseInt(hexpair, 16)` (0..255 integer), then divided by 255 in **double**.
- 3-digit form expands by char duplication (`#abc` → `aa bb cc`), NOT by `*17`.
- Alpha default is `1.0` for 3/6-digit. For 8-digit, alpha = `A/255`.
- The resulting `Color.value` is a 4-element double array in **linear-or-sRGB-agnostic 0..1 units** — the front-end does NO color-space conversion. A C# port must apply identical `int/255.0` math (e.g. `0xCC/255.0 = 0.8`), and must decide sRGB vs linear handling identically to the downstream renderer (front-end leaves bytes as-is).

---

## 6. AST node shapes (the "plans") consumed downstream

### 6.1 `Program` (root, returned by `parse`)

```js
{
  type: 'Program',
  plans: Statement[],            // chain statements, IfStmt, Break/Continue/Return (NOT VarAssign)
  render: OutputRef | null,      // {type:'OutputRef', name:'oN'} or null
  vars?: VarAssign[],            // present only if any 'let' statements
  trailingComments?: string[],   // present only if any
  namespace: {                   // always present
    imports: [{name, source:'search', explicit:true}, ...],
    default: {name, source:'search', explicit:true} | null,
    searchOrder: string[]        // copy of programSearchOrder
  }
}
```
`namespace` is a deep clone via `structuredClone` (fallback JSON, fallback manual `{...}`).

### 6.2 Statement nodes

- VarAssign: `{type:'VarAssign', name:string, expr:ExprNode, leadingComments?:string[]}`
- IfStmt: `{type:'IfStmt', condition:ExprNode, then:Statement[], elif:[{condition,then:Statement[]}], else:Statement[]|null, leadingComments?}`
- Break: `{type:'Break'}` ; Continue: `{type:'Continue'}`
- Return: `{type:'Return', value?:ExprNode}`
- Chain statement (the common case): `{chain: ChainNode[], write: SurfaceRefNode|null, write3d:{tex3d,geo}|null, leadingComments?}` — **NOTE: this wrapper has NO `type` field.** Downstream identifies it by the presence of `chain`.

### 6.3 Chain element nodes

- Call: `{type:'Call', name:string, args:ExprNode[], kwargs?:{[k]:ExprNode}, namespace?:{…fromOverride…}, leadingComments?:string[]}`
- Write: `{type:'Write', surface:SurfaceRefNode, loc:{line,col}, leadingComments?}` where SurfaceRefNode is one of `OutputRef|XyzRef|VelRef|RgbaRef|MeshRef`, or `OutputRef{name:'none'}` for the literal `none` target.
- Write3D: `{type:'Write3D', tex3d:Ref, geo:Ref, loc, leadingComments?}` where each Ref is `Ident|OutputRef|VolRef` (tex3d) / `Ident|OutputRef|GeoRef` (geo).
- Subchain: `{type:'Subchain', name:string|null, id:string|null, body:Call[], loc, leadingComments?}`
- Read: `{type:'Read', surface:ExprNode|undefined, loc, _skip?:true}`
- Read3D: `{type:'Read3D', tex3d:ExprNode|undefined, geo:ExprNode|null, loc, _skip?:true}`

### 6.4 Expression value nodes

- `{type:'Number', value:double}`
- `{type:'String', value:string}` (raw content; escapes not decoded)
- `{type:'Boolean', value:boolean}`
- `{type:'Color', value:[r,g,b,a]}` (doubles 0..1)
- `{type:'ArrayLiteral', elements:ExprNode[], loc}`
- `{type:'Func', src:string}` (arrow-fn body source text)
- `{type:'Ident', name:string}`
- `{type:'Member', path:string[]}` (dotted; ≥2 segments)
- `{type:'Chain', chain:ChainNode[]}` (only when a chain appears as a value, length>1)
- Surface refs: `OutputRef{name}`, `SourceRef{name}`, `VolRef{name}`, `GeoRef{name}`, `XyzRef{name}`, `VelRef{name}`, `RgbaRef{name}`, `MeshRef{name}` — `name` is the full lexeme (e.g. `"o0"`, `"vol3"`, `"rgba1"`).

### 6.5 Synthesized special nodes (from §4.2 transforms)

- Oscillator: `{type:'Oscillator', oscType:Node, min, max, speed, offset, seed, loc}` — each field is an ExprNode (default-filled, see §7).
- Midi: `{type:'Midi', channel, mode, min, max, sensitivity, loc}`.
- Audio: `{type:'Audio', band, min, max, loc}`.

---

## 7. Special-form parameter defaults (parser-supplied)

Resolution order for each param (all three transforms): kwarg if present, else positional by index, else default.

### 7.1 `osc(type, min, max, speed, offset, seed)` — `transformOscInvocation`
- Valid kwargs set: `{type,min,max,speed,offset,seed}`; unknown kwarg → `throw "osc() unknown parameter 'X' … Valid: type, min, max, speed, offset, seed"`.
- Defaults: `type = {type:'Member', path:['oscKind','sine']}`, `min=0`, `max=1`, `speed=1`, `offset=0`, `seed=1` (each as a `{type:'Number',value:…}` except `type` which is a Member).

### 7.2 `midi(channel, mode, min, max, sensitivity)` — `transformMidiInvocation`
- No kwarg whitelist check.
- Defaults: `mode={type:'Member',path:['midiMode','velocity']}`, `min=0`, `max=1`, `sensitivity=1`. `channel` has NO default — if unresolved → `throw "midi() requires 'channel' argument …"`.

### 7.3 `audio(band, min, max)` — `transformAudioInvocation`
- Defaults: `min=0`, `max=1`. `band` required → `throw "audio() requires 'band' argument …"`.

### 7.4 `subchain(...)` — `parseSubchainCall`
- Args: positional `subchain("name")` OR kwargs `name:`/`id:` (string values only; non-string → `throw "Expected string value for subchain KEY …"`).
- Body: `{` then ≥1 `.Call`; each element must begin with `.` (`throw "Expected '.' before chain element in subchain body …"`); empty body → `throw "Subchain body cannot be empty …"`. `}` closes.

---

## 8. Enums, aliases, ops (support modules)

### 8.1 `std_enums.js` — `stdEnums` (the built-in enum tree)

Each leaf is `{type:'Number', value:int}`. Enum member resolution downstream maps a `Member.path`
like `['oscKind','sine']` to the integer value via this tree.

`channel`: `r=0, g=1, b=2, a=3`.
`color`: `mono=0, rgb=1, hsv=2`.
`oscType`: `sine=0, linear=1, sawtooth=2, sawtoothInv=3, square=4, noise1d=5, noise2d=6`.
`oscKind`: `sine=0, tri=1, saw=2, sawInv=3, square=4, noise=5, noise1d=5, noise2d=6` (note `noise` and `noise1d` both = 5).
`midiMode`: `noteChange=0, gateNote=1, gateVelocity=2, triggerNote=3, velocity=4` (velocity is the osc/midi default).
`audioBand`: `low=0, mid=1, high=2, vol=3`.
`palette`: generated from `palettes.js` keys; **`paletteEnum[name] = {type:'Number', value: index}` where `index` is the enumeration order of `Object.keys(palettes)`** — i.e. palette enum values are positional indices into the palette table, NOT stable IDs. PARITY: the C# port must iterate the same palette source (`share/palettes.json`, camelCase keys) in the same key order to get identical indices. (Object key order in JS for string keys = insertion order = JSON property order.)

### 8.2 `enums.js` — dynamic enum registry

- Maintains `mutableEnums` (effect-contributed) and a deep-frozen `frozenEnums` (the default export).
- `mergeIntoEnums(source, mergeEnumsFn?)` deep-merges effect enum contributions then rebuilds the frozen tree.
- `deepMerge` recurses into plain nested objects but treats any object **with a `type` key as a leaf** (so `{type:'Number',value:N}` is never merged into). Frozen subtrees are cloned before merge.
- PARITY: enum resolution is *open* — effects register their own enums at load time. A C# port must collect all effect enum contributions plus `stdEnums` into one tree before resolving `Member` paths.

### 8.3 `enumPaths.js` — member-path utilities

- `normalizeMemberPath(value)`: array→filtered string segments; string→split on `.`, trim, drop empties; number→`[String(n)]`; else null.
- `pathStartsWith(path, prefix)`: prefix empty → true; length guard; element compare.
- `applyEnumPrefix(path, prefix)`: if path already starts with prefix, return copy; else try each proper suffix of prefix (i=1..) — if path starts with that suffix, prepend `prefix[0..i)`; else return `prefix.concat(path)`. (Used to qualify a short enum member with its enum name.)
- `stripEnumPrefix(path, prefix)`: inverse — strips prefix or a matching suffix-of-prefix from the front of path.

### 8.4 `effectAliases.js` — deprecated effect names

Registry `{ [oldOpName]: newName }`. `registerEffectAlias(old,new)`. `checkEffectAlias(opName)` →
`null` or warning string `"effect 'X' is deprecated, use 'Y' instead. Aliases will be removed on 2026-09-01."`
(`oldName` = last `.`-segment of opName). Empty by default; populated by effect modules at load.

### 8.5 `paramAliases.js` — deprecated parameter names

`ALIAS_EOL_DATE = '2026-09-01'`. Registry `{ [opName]: {oldParam:newParam} }`.
`resolveParamAliases(opName, kwargs)` **mutates kwargs in place**:
- For each alias `old`→`new`: skip if `old` not in kwargs; if `new` not present, copy value to `new`; **always delete `old`**; **always push a warning** (even when both present, in which case `new`'s value wins and `old` is dropped). Returns warning strings.

### 8.6 `ops.js`

Trivial registry: `export const ops = {}`; `registerOp(name, spec)` sets `ops[name]=spec`. Populated by effect modules.

### 8.7 `diagnostics.js` — diagnostic code table

```
L001 lexer/error  Unexpected character
L002 lexer/error  Unterminated string literal
P001 parser/error Unexpected token
P002 parser/error Expected closing parenthesis
S001 semantic/error   Unknown identifier
S002 semantic/warning Argument out of range
S003 semantic/error   Variable used before assignment
S004 semantic/error   Cannot assign null or undefined
S005 semantic/error   Illegal chain structure
S006 semantic/error   Starter chain missing write() call
S007 semantic/warning Deprecated parameter alias
S008 semantic/warning Deprecated effect
R001 runtime/error    Runtime error
```
(These are downstream-validator codes; the parser itself throws raw `SyntaxError` with embedded `at line L col C` text, NOT diagnostic codes.)

---

## 9. Error formatting (`error-formatter.js`)

- `parseLocation(msg)`: regex `/at line (\d+) col(?:umn)? (\d+)/` → `{line,col}` or null.
- `extractMessage(msg)`: strips trailing `/\s+at line \d+ col(?:umn)? \d+$/`.
- `formatDslError(source, error, {contextLines=2})`: builds a multi-line caret display:
  - Header `SyntaxError: <core>` and ``  --> line L, column C``.
  - `contextLines` lines before, the error line, a pointer line `(lineNumWidth+3) spaces + (col-1) spaces + "^-- error here"`, then `contextLines` after. Line numbers right-padded to width of `min(L+contextLines, lines.length)`.
- `isDslSyntaxError(error)`: `error instanceof SyntaxError && parseLocation(msg)!==null`.

PARITY NOTE: the caret column = `errorCol - 1` spaces after the gutter. This depends on the lexer's
per-code-unit column counting being identical.

---

## 10. Consolidated PARITY HAZARDS for an HLSL/C# re-implementation

1. **Constant-folded numeric expressions (§4.4).** All `+ - * /` on literals are evaluated at parse time in JS IEEE-754 double and frozen into `Number.value`. Operands MUST be numeric literals/`Math.PI`/parenthesized — identifiers/members in arithmetic are a hard syntax error (`"Expected number"`). The C# port must fold in `double`, left-to-right, with the same precedence, and store the resulting double verbatim. `Math.PI = 3.141592653589793`.
2. **Color math (§5).** `parseInt(pair,16)/255` in double; 3-digit expansion by char duplication (`#abc`→`0xaa,0xbb,0xcc`); alpha default 1.0; 8-digit alpha `A/255`. No sRGB/linear conversion in front-end — color values are raw 0..1 doubles; downstream renderer decides color space. Y-flip / coordinate origin are NOT a front-end concern (no coordinates here) but must match downstream.
3. **Palette enum indices are positional (§8.1).** `palette.<name>` resolves to the *array index* of that key in `Object.keys(palettes)`. Identical JSON key order is required for identical integers. This is the single biggest cross-language drift risk for enums.
4. **Dynamic namespace + enum registries (§3.2, §8.2).** `VALID_NAMESPACES` and the enum tree are mutated at runtime by loaded effect modules. The parser's `search` validation and downstream `Member` enum resolution depend on registration order/content. The C# port must populate these registries from the same effect manifest before parsing.
5. **String escapes NOT decoded (§1.4 rules 15/16).** `STRING.value` is the raw inter-delimiter text; `\n` etc. survive as 2 chars. Triple-quoted strings ignore escapes entirely. Match this (do not unescape in the lexer).
6. **Token disambiguation by fixed-offset char compares (§1.4).** Surface-prefix detection (`vol`/`vel`/`geo`/`xyz`/`rgba`/`mesh`/`o`/`s`) requires the prefix letters AND an immediate digit; otherwise the lexeme becomes a plain identifier. `vol`-before-`vel` ordering and HEX length gating {3,6,8} must be replicated exactly.
7. **Column/line counting (§1.2, §9).** Per-UTF-16-code-unit; tabs=1; affects only error carets, but tests may assert exact `line`/`col`.
8. **`render` terminates the program (§3.1d).** Anything after a `render(...)` (except trailing comments) is NOT parsed; loop breaks. A duplicate `render` (`render` appearing while one is already set) throws.
9. **`search` is mandatory and position-restricted (§3.1c,§3.2,§3.3.1).** Missing → throw at end; appearing after statements → throw; appearing twice → throw; inside a statement (e.g. block) → throw.
10. **Chain-statement wrapper has no `type` (§6.2).** Downstream must detect it by `chain` presence, not by `type`. Only a *terminal* `Write`/`Write3D` populates `write`/`write3d`.
11. **`{type:'Chain'}` wrapper appears ONLY for multi-element chains used as values (§4.5 IDENT case 2).** Single-element chains return the lone node unwrapped. Top-level statements always store the raw `chain` array, never wrapped.
12. **`osc` is overloaded (§4.2).** Heuristic decides value-oscillator vs `synth.osc` effect. Replicate the exact 4-way OR condition or programs will misparse.
13. **Mixed positional+keyword args forbidden (§4.2.4); trailing comma allowed.**
14. **Inline namespace `a.b()` forbidden (§4.2.2).** Member-dot-then-call lookahead (`hasCallAfterDot`) distinguishes `oscKind.sine` (member) from `foo.bar()` (illegal inline ns) by whether a `LPAREN` follows the dotted run, and whether the call is the *head* (illegal) vs a dotted enum (allowed).
