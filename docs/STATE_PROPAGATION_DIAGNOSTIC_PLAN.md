Ferry — State Propagation Diagnostic Plan
Audience: coding agent with repository access Phase: investigation only Output: an evidence report, not a patch

0. Rules of engagement
Do not fix anything during this phase. Not even "obvious" fixes. A premature fix destroys the evidence that distinguishes root causes.
Do not delete or rewrite existing logging. Add new logging alongside it.
All instrumentation added in this phase must be prefixed FERRY_DIAG so it can be removed with a single search.
Where this document uses a name like SessionRegistry or SessionEstablished, treat it as a role description, not a real symbol. Find the actual thing in the repo that plays that role and record its real name in your report. If no component plays that role, say so explicitly — that absence is itself a finding.
If any step is impossible (component doesn't exist, code path not found), report "NOT FOUND" rather than substituting a guess. A missing component is a high-value result.
1. Repository mapping
Before running anything, produce a map. For each item below, record: file path, symbol name, and a one-line description. If you cannot find it, write NOT FOUND.

1.1 Linux daemon
Role	What to look for
mDNS advertiser / browser	Where the DNS-SD service is registered and where peer records are resolved
Inbound connection acceptor	The socket/listener accept path — where a remote-initiated connection first enters the daemon
Outbound connection initiator	The path used when Linux starts a connection to a peer
Trust store	Persistent storage of paired peer identity keys; the lookup function; the on-disk path
Pairing / SAS logic	Where a pairing request is created, where SAS digits are computed, where user confirmation is awaited
Handshake / session establishment	Where the authenticated secure session is completed
Session registry	Whatever structure holds live sessions. Critical: determine whether this is a real registry or just a set of per-connection objects owned by the transport layer with no central index.
IPC server surface	Method/command definitions and event/signal definitions exposed to the UI
Transfer subsystem entry point	The function the UI would call to send files to a peer, and what identifier it takes (peer ID? session ID? socket handle?)
1.2 Linux GTK UI
Role	What to look for
IPC client setup	Where the connection to the daemon is established at startup
Initial state query	Any call at startup that asks the daemon for current state
Event subscription	Any subscription/signal-connect to daemon lifecycle events
UI-side state model	Any in-process store of peers/sessions the UI maintains itself
Device list rendering	Where peers are turned into list rows
Send Files / Send Folder control	Where the control is created, and the exact predicate that decides enabled/visible
Connection status widget	Whatever renders "connected / disconnected / discovering"
1.3 Android
Role	What to look for
mDNS discovery	NSD/JmDNS usage; where resolved peers are emitted
Trust store	Persisted known-peer keys; the lookup; the storage path/DataStore/DB
Pair-vs-reconnect branch	The decision point where code chooses "run pairing" vs "resume with trusted peer". This is the single most important line of code in the whole investigation.
Handshake / session establishment	Client side of the secure session
Session state holder	ViewModel/repository that the Compose UI observes
UI state enum/sealed class	Whatever represents Idle / Discovering / Found / Pairing / Connected
Cached peer loading	Any code that populates the peer list from disk/prefs before or instead of discovery
1.4 Shared protocol
Where are IPC message types/schemas defined? Is there a single shared definition, or are daemon and UI defining them independently?
Is there a protocol version constant? Is it checked?
Where are the wire-level protocol messages for the Android↔Linux link defined, and is there a version handshake?
Deliverable for section 1: a table of ~25 rows. Do not proceed until it is complete.

2. Static inspection questions
Answer each from code reading alone, before instrumenting. Cite file:line for each answer.

2.1 Session registry reality check
Is there a single collection that holds all live authenticated sessions in the daemon?
Is it populated on both the inbound-accept path and the outbound-initiate path? Trace both paths separately and state where each one writes.
What is the key type? (peer fingerprint / session UUID / socket fd / device name?)
Is there any IPC method that reads from it?
2.2 IPC surface audit
List every IPC method the daemon exposes. Mark which ones return session state.
List every IPC event/signal the daemon emits. Mark which ones relate to session lifecycle.
For each session-lifecycle event: find the emit site. Is it on the inbound path, the outbound path, or both?
Is there any event emitted when a session ends?
2.3 UI subscription audit
Does the GTK UI subscribe to each event found in Q6? List subscribed vs unsubscribed.
Compare the exact string names of emitted events vs subscribed events, character by character. Also compare interface/bus/namespace names if applicable.
Does subscription happen before or after the initial query? Is there a window where an event could be missed?
Does the UI call any state-query method at startup? If yes, which one, and does it cover sessions or only known/discovered peers?
2.4 Send Files predicate
Write out the exact boolean expression that enables the Send Files control.
Classify each term in that expression as depending on: discovery state, trust state, or session state.
Does the control's action handler take a peer identifier or a session identifier?
2.5 Android branch
Write out the exact condition that decides pairing vs trusted-reconnect.
What is compared? Device name, IP, service instance name, or identity public key / fingerprint? Anything other than a cryptographic identity is a correctness bug independent of this investigation — flag it.
Does the Android UI have a distinct state for "discovering"? Is it ever rendered, or is it skipped because cached peers are emitted synchronously before discovery starts?
3. Instrumentation
3.1 Correlation
Every diagnostic log line must carry a correlation key. Use the peer identity fingerprint (first 8 hex chars is enough) because it is the one value that exists on both sides at every stage. Once a session ID exists, log both.

Format (keep identical on all three components so logs can be merged and sorted):

FERRY_DIAG <component> <event> peer=<fp8> session=<sid|none> t=<epoch_ms> <key=value ...>
<component> is one of android, daemon, gtk.

3.2 Android events to add
Event	Where	Extra fields
DISCOVERY_STARTED	mDNS browse begins	—
PEER_RESOLVED	service resolved	addr, port, src=mdns
PEER_FROM_CACHE	a peer is emitted from persisted storage	src=cache, age_ms
TRUST_LOOKUP	trust store queried	result=trusted|unknown, matched_on=key|name|addr
PAIRING_BRANCH	the decision point	decision=pair|reconnect, reason=<literal condition outcome>
HANDSHAKE_START	before first handshake byte	role=initiator
SAS_DISPLAYED	SAS shown to user	sas=<digits>
SESSION_ESTABLISHED	handshake complete	session=<sid>
UI_STATE	every emission to the Compose state holder	state=<enum name>
3.3 Linux daemon events to add
Event	Where	Extra fields
INBOUND_ACCEPTED	socket accept	remote_addr
TRUST_LOOKUP	trust store queried	result=trusted|unknown, matched_on=
PAIRING_REQUEST_CREATED	pairing request object made	—
PAIRING_SKIPPED	the branch that bypasses pairing	reason=<literal condition outcome>
SAS_COMPUTED	SAS digits derived	sas=<digits>
SESSION_ESTABLISHED	handshake complete	session=<sid>, direction=inbound|outbound
SESSION_REGISTERED	write into the registry	registry=<type name>, size_after=<n>
IPC_EMIT	immediately before every event emission	event=<name>, subscribers=<n if available>
REGISTRY_SNAPSHOT	see 3.5	count=<n>, peers=[fp8,...]
3.4 GTK UI events to add
Event	Where	Extra fields
IPC_CONNECTED	IPC client ready	—
IPC_SUBSCRIBED	each subscription registered	event=<name>
INITIAL_QUERY_SENT	startup state query	method=<name>
INITIAL_QUERY_RESULT	its response	sessions=<n>, peers=<n>
IPC_RECV	every inbound IPC message, including unrecognised ones	event=<name>, handled=true|false
UI_MODEL_UPDATED	UI state store write	peers=<n>, sessions=<n>
SEND_PREDICATE	each evaluation of the Send Files enable condition	enabled=, plus each term of the expression individually
IPC_RECV with handled=false is the highest-value line in this entire plan. Make sure the catch-all branch exists even if the current code silently drops unknown messages.

3.5 Registry probe
Add a temporary, read-only IPC method (e.g. Diag.DumpSessions) on the daemon that returns the raw registry contents: count, keys, per-session direction, peer fingerprint, trust flag, established timestamp. Also log REGISTRY_SNAPSHOT every 5 seconds while at least one session exists.

This is what lets you answer question 7 ("does the daemon have the session while the UI does not") with certainty rather than inference. Mark it clearly for later removal.

4. Controlled experiments
Run in order. Capture all three log streams for each. Between experiments, fully restart daemon, GTK UI, and Android app unless the experiment says otherwise.

First, record the on-disk location and current contents (key count) of both trust stores. Back them up before any wipe.

#	Setup	Action	Primary question
E0	Everything as-is	Android → Connect	Baseline. Reproduce all six symptoms with logs.
E1	Wipe Android trust store only	Android → Connect	Does Android now take the pairing branch? Does Linux still skip? (Asymmetric trust — reveals whether each side decides independently.)
E2	Wipe Linux trust store only	Android → Connect	Does the daemon create a pairing request? Does it reach GTK?
E3	Wipe both trust stores	Android → Connect	The decisive pairing test. If SAS appears on both sides, pairing is healthy and #4 is not a bug. If no SAS with both stores empty, pairing generation or its UI path is genuinely broken.
E4	From E3's established session, kill and restart only the GTK UI	observe	Does the UI recover state? Isolates missing-initial-query from missing-subscription.
E5	Established session, then restart only the daemon	observe	Does GTK notice the session died? Tests session-end events and stale UI.
E6	Start GTK UI before the daemon	connect Android	Does the UI ever subscribe successfully? Tests subscribe-before-ready races.
E7	Initiate from Linux → Android if the UI allows it at all	observe	Does a Linux-initiated session appear in the UI when an Android-initiated one does not? Isolates direction-dependent registration.
E8	Established session	Attempt a transfer Android → Linux	Confirms the session is genuinely live even while the UI shows nothing.
E9	Established session	Call Diag.DumpSessions directly (CLI/IPC tool, not via UI)	Ground truth on registry contents.
Restore the trust stores from backup when finished.

5. Distinguishing the five conditions
Use these signatures. Each is defined by a combination of log lines, not a single one.

Trusted reconnect (legitimate)
android TRUST_LOOKUP result=trusted matched_on=key
android PAIRING_BRANCH decision=reconnect
daemon TRUST_LOOKUP result=trusted matched_on=key
daemon PAIRING_SKIPPED reason=peer_trusted
No SAS_* on either side
SESSION_ESTABLISHED on both → Correct behaviour. Symptom #4 is not a bug in this case.
New-device pairing (expected for untrusted)
TRUST_LOOKUP result=unknown on both sides
daemon PAIRING_REQUEST_CREATED
SAS_COMPUTED / SAS_DISPLAYED with matching digits
SESSION_ESTABLISHED only after user confirmation → If E3 does not produce this, pairing is broken.
Discovery-only (no session)
PEER_RESOLVED present
No HANDSHAKE_START, no SESSION_ESTABLISHED
Android UI nonetheless shows a device → Android is rendering a discovery result as if it were a connection.
Authenticated session present
SESSION_ESTABLISHED on both sides with the same fingerprint
daemon SESSION_REGISTERED size_after≥1
Diag.DumpSessions returns a non-empty set
A transfer succeeds (E8)
UI desynchronisation
All four markers of "authenticated session present" hold, and
gtk UI_MODEL_UPDATED sessions=0, or no UI_MODEL_UPDATED at all after the session was established → This is the state-propagation failure. Proceed to the decision tree.
Stale/cached peer mistaken for live state
android PEER_FROM_CACHE src=cache fires, and
android UI_STATE state=Connected (or equivalent) fires with no preceding SESSION_ESTABLISHED for that fingerprint
Or: the timestamp of the UI "connected" state precedes HANDSHAKE_START → Android UI derives connection state from the peer list rather than the session.
Additional check: does Android ever show a device whose PEER_RESOLVED never fired in this run? If yes, cached peers are being displayed without live verification.

6. Decision tree
Evaluate in order; stop at the first match.

A. Daemon has the session, GTK never receives any message about it

Evidence: daemon SESSION_REGISTERED + DumpSessions non-empty + no gtk IPC_RECV for a session event around that timestamp, and daemon IPC_EMIT event=<x> did fire.

→ Root cause: transport/naming mismatch. The event is emitted onto a channel the UI is not listening on, or under a name the UI does not match. Cross-check Q10. Also check subscriber count in IPC_EMIT — zero subscribers confirms it.

Sub-case: if IPC_EMIT did not fire at all, the daemon registers sessions without notifying anyone → emit site missing on the inbound path (see D).

B. GTK receives the message but does not act on it

Evidence: gtk IPC_RECV event=<x> handled=false, or handled=true with no subsequent UI_MODEL_UPDATED.

→ Root cause: schema/handler mismatch. Either the payload fails to deserialise into the UI's expected shape (field renamed, nesting changed, optional vs required), or the handler is registered but its body filters the event out (e.g. matches on a peer already in a locally-maintained list, and the inbound peer isn't in it). Inspect the handler body and the payload bytes.

C. GTK acts on live events but starts blank and never recovers

Evidence: E4 — after restarting only the UI, INITIAL_QUERY_SENT is absent, or INITIAL_QUERY_RESULT sessions=0 while DumpSessions is non-empty.

→ Root cause: no initial state query, or a query that returns discovered peers rather than live sessions. The UI is event-only and therefore permanently blind to any session that began before it started. Note this can coexist with A or B; if E0 also shows no events, fix A/B first.

D. Sessions are registered only for Linux-initiated connections

Evidence: E7 shows a Linux-initiated session appearing in the UI while E0's Android-initiated one does not; or SESSION_REGISTERED fires on the outbound path but not the inbound one; or direction=inbound sessions are absent from DumpSessions.

→ Root cause: asymmetric session registration. The inbound accept path completes a handshake and hands the socket straight to the transfer subsystem without registering it centrally. This single cause explains symptoms #5, #6 and #7's Linux-side counterpart, and is the most likely candidate given that transfers work while the UI knows nothing.

E. No authoritative registry exists at all

Evidence: Q1 answered NO — live sessions exist only as objects owned by connection handlers, with no central index; DumpSessions had to be implemented by walking transport internals.

→ Root cause: architectural gap, not a bug. There is nothing for the UI to query. This is the case in which the proposed registry is the fix rather than a refactor. Report it as such.

F. Daemon does not have the session either

Evidence: transfers succeed (E8) but DumpSessions is empty and SESSION_ESTABLISHED never fires in the daemon.

→ Root cause: the "session" is not a modelled concept on the Linux side — the transfer path authenticates per-connection and discards the result. Treat as a more severe form of E.

G. Pairing genuinely broken

Evidence: E3 (both trust stores wiped) still shows daemon PAIRING_SKIPPED or no PAIRING_REQUEST_CREATED.

→ Follow the reason field. Two likely shapes: (i) the trust lookup returns trusted incorrectly — check matched_on; if it matched on name or address rather than key, that is the bug and it is a security defect, escalate immediately; (ii) the pairing request is created but its UI notification follows the same broken path as session events, in which case G collapses into A/B/C and is fixed by the same change.

H. Android shows connected without a session

Evidence: the stale-cache signature in §5.

→ Root cause: Android UI state derived from peer list rather than session state. Independent of all Linux-side causes; can be true simultaneously. Explains symptom #7.

Causes A–G are Linux-side and roughly mutually exclusive at the top level; H is orthogonal and may co-occur with any of them. Report all that apply.

7. Answering the remaining specific questions
Q13 — is Send Files derived from session state? Take the expression from Q14. It is correct only if it is gated on the presence of an ESTABLISHED session for that peer. It is wrong if it is gated on: peer present in discovery list; peer in trust store; peer reachable; a boolean flag set by the UI when the user clicked something. Confirm empirically: in E8, while a transfer is actively succeeding, log SEND_PREDICATE and record which term is false.

Q14 — would fixing propagation fix Send Files without duplicating state? It does if and only if the control can be derived purely from data supplied by the daemon. Check whether the UI's peer/session model has any field the daemon does not supply. If the UI maintains its own notion of "currently connected peer" set by user action, that field is duplicate state and must be identified now, so the later fix deletes it rather than adding a second source of truth beside it. List every such field.

Q12 — IPC mismatches. Beyond name comparison (Q10): capture raw payload bytes for one session event and manually deserialise using the UI's schema. Check field names, optionality, enum encodings (string vs int), and whether the daemon and UI share one schema definition or two copies that have drifted. If two copies, diff them.

8. Expected architecture after the fix
Record this as the target, but do not implement yet.

Android                         Linux daemon                    GTK UI
-------                         ------------                    ------
mDNS browse
  ↓
peer resolved ──────────────→ (advertised service)
  ↓
trust lookup (by identity key)
  ↓
 ┌─ unknown ─→ pairing ──────→ pairing request created
 │             SAS both ends   → PairingRequested event ──────→ modal prompt
 │             user confirms  ←──────────────────────────────── confirm
 │             trust persisted both sides
 └─ trusted ─→ skip pairing
  ↓
handshake ──────────────────→ handshake completes
                                     ↓
                              SESSION REGISTERED
                              (single authoritative registry,
                               keyed by identity fingerprint,
                               written by BOTH inbound and
                               outbound paths)
                                     ↓
                              SessionEstablished event ───────→ update model
                                     ↓
                              ListSessions() ←───────────────── startup query
                                                                     ↓
                                                              render connected peer
                                                                     ↓
                                                              Send Files enabled
                                                              iff session ESTABLISHED
Invariants the fix must satisfy:

One writer. The daemon's registry is the only source of truth for session state. The GTK UI holds a projection, never an authority.
Symmetric registration. Inbound and outbound paths converge on the same registration call.
Query + subscribe. The UI subscribes first, then queries, then reconciles — so no event is lost in the gap.
Keyed by identity. Fingerprint, not name or address.
Lifecycle completeness. Established, ended, and failed all emit. The UI must be able to return to a disconnected state without a restart.
Derived controls. Send Files/Send Folder enablement is a pure function of registry state for that peer.
Android symmetry. Android's UI state is likewise derived from its session layer, with Discovering / Found / Pairing / Connected as distinct rendered states.
9. Report format
Produce a single document containing:

The component map from §1 (real symbol names, NOT FOUND where applicable).
Answers to all 18 static questions with file:line citations.
Merged, time-sorted log excerpts for E0 and E3 at minimum — annotated, showing the exact point where the chain breaks.
Which decision-tree branch(es) the evidence supports, and the specific lines proving it.
Any contradictions — evidence that fits no branch, or fits two. Do not resolve these by guessing; report them.
The list of duplicate-state fields from §7/Q14.
Any security concerns found (particularly trust matching on anything other than identity key).
A list of every FERRY_DIAG addition and the temporary Diag.DumpSessions method, for clean removal.
Do not propose or write the fix in this report. The fix is decided after the evidence is reviewed.