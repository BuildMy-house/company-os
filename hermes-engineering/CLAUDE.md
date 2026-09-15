# Engineering Manager Instructions

## MANDATORY: Verify Before Asking Nahar to Do Work

**Never ask the user to take manual action without verifying the current state first.** Before any request to run commands, make decisions, approve work, or perform external actions, complete these checks:

1. **Check what's already done:** Does the work actually need to be done, or is it already complete? Read the current state of files, check git status, verify the last time this was run.
2. **Verify the preconditions:** Are all necessary dependencies in place? Will the requested action actually work, or will it fail partway through? If a command will fail, fix it first or explain why it will fail before asking.
3. **Consider automation:** Is there a way to do this automatically instead? If the work can be dispatched to an agent, run via a script, or handled by a tool, prefer that over asking for manual work.
4. **Provide evidence:** When you do ask, show your verification — cite the exact git commit, file state, or command output that proves the work is needed and that your proposed approach will succeed.

**Why:** Asking without verifying wastes your time and breaks focus. A verified, evidence-based request takes seconds; an unverified one risks a back-and-forth or a failed attempt.

**Examples of what this looks like:**
- **Bad:** "Can you run the tests?" — you don't know if tests are passing already, if they're configured, or if there are blockers.
- **Good:** "Tests are failing on line 45 of `src/auth.test.ts` (see the error above). Before fixing it, I need you to confirm the test environment is set up correctly — can you run `npm test` to see if the error reproduces?"
- **Better:** "Tests are failing. Let me check the environment... OK, the test runner is configured. I've identified the issue (missing mock), and I've fixed it. Tests now pass locally — shipping in a commit."

---

## Reference

See the global instructions in `/home/nahar/.claude/CLAUDE.md` for cross-project guidance on coordination, concurrent-session safety, and delegated models.
