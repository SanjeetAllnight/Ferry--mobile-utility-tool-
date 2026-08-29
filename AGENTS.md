# AGENTS.md — Ferry AI Agent Operating Manual

Welcome to **Ferry**. This document is the permanent, mandatory operating manual for all AI agents working on this codebase.

Because development is conducted across stateless agent sessions, you must adhere strictly to the protocols, boundaries, and documentation requirements established here.

---

## 1. Mandatory Pre-Flight Checklist

Before writing code, proposing architectural modifications, or executing tasks:

1. **Read this file (`AGENTS.md`) first** to understand current operating constraints.
2. **Read [`docs/PROJECT_STATE.md`](file:///home/sanjeet/Projects/Ferry/docs/PROJECT_STATE.md)** to obtain the exact current phase, milestone, active components, and verified status.
3. **Inspect the existing implementation** (`linux/`, `android/`, `docs/`) before creating new components or files.
4. **Read [`docs/ARCHITECTURE.md`](file:///home/sanjeet/Projects/Ferry/docs/ARCHITECTURE.md)**, **[`docs/PROTOCOL.md`](file:///home/sanjeet/Projects/Ferry/docs/PROTOCOL.md)**, and **[`docs/SECURITY.md`](file:///home/sanjeet/Projects/Ferry/docs/SECURITY.md)** whenever working on networking, serialization, or authentication.

---

## 2. Core Operating Rules

### A. Respect Phase Boundaries
* Ferry is developed in strict sequential phases.
* **Do NOT implement features from later phases** unless explicitly commanded by the user.
* If a task touches an area planned for a future milestone (e.g. mDNS discovery in Phase 1, or clipboard sync in Phase 2), implement only the necessary interfaces or mocks appropriate for the current phase.

### B. Avoid Redundancy and Architectural Churn
* **Do NOT recreate existing functionality.** Re-use established core modules, models, and utility classes.
* **Do NOT make unnecessary architectural changes.** If an existing pattern works, extend it rather than refactoring to an unrequested framework or paradigm.
* If a major architectural change is genuinely required, document it in [`docs/DECISIONS.md`](file:///home/sanjeet/Projects/Ferry/docs/DECISIONS.md) with rationale, alternatives, and consequences.

### C. Protocol Invariance
* **Never silently change protocol behavior or message formats.**
* The communication protocol between Linux and Android is versioned (defined in [`docs/PROTOCOL.md`](file:///home/sanjeet/Projects/Ferry/docs/PROTOCOL.md)).
* All wire changes must be backward-compatible or explicitly version-bumped and documented.

### D. Documentation is Mandatory
* Whenever you change behavior, add a configuration, or introduce an API, **update the corresponding documentation** in `docs/`.
* **Always update [`docs/PROJECT_STATE.md`](file:///home/sanjeet/Projects/Ferry/docs/PROJECT_STATE.md)** at the end of every meaningful task with:
  - current milestone status
  - verified test and build results
  - known limitations or bugs
  - recommended next steps

### E. Automate Routine Tasks
* Prefer executing environment inspections, dependency configuration, builds, tests, and ADB installations directly via terminal tools rather than asking the user to do so manually.
* Only ask the user when genuine human decisions, external credentials, or physical device actions are required.

### F. Preserve Working Code & Reproducibility
* Always run automated tests and build commands after modifications.
* Ensure you leave the repository in a clean, reproducible state with zero broken builds.
* Clearly report genuine blockers rather than silently bypassing requirements or faking success.

---

## 3. Standard Verification Commands

### Linux Verification
```bash
# Run unit tests
python3 -m unittest discover -s linux/tests -v

# Run Linux application in UI mode
python3 -m ferry_linux

# Run Linux core service in daemon mode
python3 -m ferry_linux --service
```

### Android Verification
```bash
# Run unit tests
cd android && ./gradlew testDebugUnitTest

# Assemble debug APK
cd android && ./gradlew assembleDebug

# Install to connected device
adb install -r android/app/build/outputs/apk/debug/app-debug.apk

# Launch main activity
adb shell am start -n dev.ferry.app/.MainActivity
```

---

## 4. Key References

* **Current Status & Context**: [`docs/PROJECT_STATE.md`](file:///home/sanjeet/Projects/Ferry/docs/PROJECT_STATE.md)
* **System Architecture**: [`docs/ARCHITECTURE.md`](file:///home/sanjeet/Projects/Ferry/docs/ARCHITECTURE.md)
* **Wire Protocol Specification**: [`docs/PROTOCOL.md`](file:///home/sanjeet/Projects/Ferry/docs/PROTOCOL.md)
* **Security Model & Cryptography**: [`docs/SECURITY.md`](file:///home/sanjeet/Projects/Ferry/docs/SECURITY.md)
* **Developer Setup & Workflows**: [`docs/DEVELOPMENT.md`](file:///home/sanjeet/Projects/Ferry/docs/DEVELOPMENT.md)
* **Testing Strategy & Suites**: [`docs/TESTING.md`](file:///home/sanjeet/Projects/Ferry/docs/TESTING.md)
* **Architectural Decisions (ADRs)**: [`docs/DECISIONS.md`](file:///home/sanjeet/Projects/Ferry/docs/DECISIONS.md)
