# CLAUDE.md — Engineer v3 Production Override
# Scope: All agentic coding tasks in this project.
# These directives take precedence over default brevity/simplicity heuristics.

---

## 0. TASK INTAKE PROTOCOL
Before writing a single line of code:
1. Restate the task in your own words. Confirm scope explicitly.
2. List every file you expect to touch. If >5 files, decompose into phases now.
3. State any ambiguities. Do not resolve ambiguities silently — surface them.
4. If the task requires understanding existing code, READ IT FIRST. Never plan 
   against assumed file contents.

---

## 1. STEP 0 — DEAD CODE REMOVAL (mandatory for files >300 LOC)
Before any structural refactor:
- Remove unused imports, dead props, orphaned exports, debug logs.
- Commit this as a standalone, isolated change.
- Only then begin the actual task with a clean token budget.
Rationale: Dead code accelerates context compaction. This is not optional.

---

## 2. PHASED EXECUTION
- Never execute multi-file changes in a single pass.
- Maximum 5 files per phase.
- After each phase: run verification (see §4), report results, wait for approval.
- Do not proceed to Phase N+1 without explicit go-ahead.

---

## 3. CODE QUALITY STANDARD
Ignore default heuristics: "simplest approach," "avoid refactoring beyond scope."

Apply this standard instead:
"Would a senior engineer with high standards reject this in code review?"

If yes: propose the structural fix, explain the tradeoff, implement with approval.
Specifically:
- Duplicated state → consolidate
- Inconsistent patterns → normalize
- Band-aid fixes on broken architecture → flag and fix root cause
- Any TODO/FIXME in touched files → surface to user, do not silently ignore

---

## 4. MANDATORY VERIFICATION LOOP
You are FORBIDDEN from reporting task completion without:

1. `npx tsc --noEmit` — fix ALL type errors before proceeding
2. `npx eslint . --quiet` — fix ALL lint errors before proceeding  
3. Re-read every edited file and confirm the diff matches intent
4. If tests exist: run them. Report pass/fail count explicitly.

If no type-checker is configured: state this explicitly. Do not claim success by 
silence.

If verification fails: fix errors silently, re-run, report final clean state only.
Never report partial success.

---

## 5. CONTEXT DECAY PROTOCOL
After 10+ messages in a session:
- Re-read any file before editing it. Do not trust in-context memory.
- If you suspect compaction occurred: state it. Re-read affected files.
- For sessions spanning >15 messages on a large codebase: checkpoint by 
  summarizing current state before continuing.

---

## 6. FILE READ PROTOCOL
- Hard cap: ~2,000 lines per read. Assume truncation on any file >500 LOC.
- For files >500 LOC: read in chunks using offset/limit parameters.
- After chunked reads: explicitly confirm you have read the full file before editing.
- Never edit a file you have not fully read in the current session phase.

---

## 7. TOOL RESULT INTEGRITY
- Tool results >50K chars may be silently truncated to a short preview.
- If search/grep returns suspiciously few results: re-run scoped to single 
  directories. State when truncation is suspected.
- Never treat a single grep as exhaustive on a large codebase.

---

## 8. RENAME/REFACTOR SAFETY CHECKLIST
For any rename, signature change, or interface modification, run ALL of:
- [ ] Direct calls and references
- [ ] Type-level references (interfaces, generics, type aliases)
- [ ] String literals containing the identifier
- [ ] Dynamic imports and require() calls
- [ ] Re-exports and barrel file entries (index.ts)
- [ ] Test files and mock definitions
- [ ] Config files (webpack aliases, tsconfig paths)

Do not report completion until all are checked.

---

## 9. SUB-AGENT ORCHESTRATION
For tasks touching >5 independent files:
- Decompose into independent batches (5-8 files each)
- Assign each batch to a sub-agent with explicit scope: files, task, 
  verification requirement
- Sub-agents must not assume shared state — pass all required context explicitly
- Aggregation step: after all sub-agents complete, run full verification 
  (§4) on the combined result

---

## 10. SELF-CHECK LOOP
Before reporting any result:
1. Re-read the original task statement
2. Confirm every requirement is addressed
3. If anything is unaddressed: complete it or explicitly flag it as out of scope
4. State confidence level: High / Medium / Low, with reason if Medium or Low

---

## 11. PROMPT CACHE AWARENESS
To maximize cache hit rate and reduce cost:
- Keep stable context blocks (this CLAUDE.md, file structures, interfaces) 
  at the top of context, unchanged between messages
- Place volatile content (current task, diffs, errors) at the bottom
- Do not restate stable context in volatile sections

---

## 12. ANTI-PATTERNS — NEVER DO THESE
- ❌ Report "Done!" without running verification
- ❌ Edit a file without re-reading it first in the current phase
- ❌ Run a single grep and assume it caught all references
- ❌ Silently skip a phase requirement because it seems unnecessary
- ❌ Resolve task ambiguity by making an assumption — surface it
- ❌ Touch >5 files in one phase
- ❌ Proceed to next phase without explicit user approval
