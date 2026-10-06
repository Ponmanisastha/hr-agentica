# Your HR policy documents

Put your organisation's HR policies in this folder: `.pdf`, `.docx`, `.md` or `.txt`, in subfolders if you like.
The policy and leave agents answer from them and cite the file and section. While this folder has no documents,
the sample handbook in `data/policy_handbook.md` is used instead; as soon as you add one, it replaces the sample.

Three ways to add or change them:

- In the web console, open **Policies** and use **Upload documents** (HR and admin).
- `python app.py policies add "Leave Policy 2026.pdf" "Code of Conduct.docx"`
- Copy files here. The `policy_watch` trigger re-indexes within five minutes while `python app.py serve` runs,
  or run `python app.py policies reindex` to do it now.

An upload (web or `policies add`) is staged as the next version of that document and waits for HR approval: the
agent lists the sections and leave rules that would change and any rule another document states differently.
Approving publishes it and archives the previous version; `python app.py policies rollback NAME VERSION` (or
**Restore this version**) brings an old one back, and `policies remove NAME` retires a document. Files copied in
directly are trusted and recorded as a new version at the next re-index. When two documents state the same rule
differently, the most recently approved one wins.

`.pending/` holds uploads waiting for approval and `.archive/<file>/v<N>/` every published version. Both are
skipped when answering questions and git-ignored.

`python app.py policies` lists what was indexed, document by document and section by section.

How documents are split: Markdown by `## ` headings; PDF, Word and text files by heading-like lines
("3. Casual leave", "2.1 Sick leave", "SICK LEAVE"); anything without headings in ~1,200-character parts.
Clear headings give the best citations.

The leave rules the agent enforces (notice days, the auto-approval limit, the casual-leave cap, when a medical
certificate is needed, carry-forward, monthly accrual, notice periods) are read from these documents too, from
sections whose titles mention annual, sick, casual, maternity, paternity, notice, home or reimbursement. The
phrasing it looks for is in `RULE_PATTERNS` in `hrai/knowledge/kag.py`; `python app.py policies` plus
`python app.py ask "Who approves leave for <name>?"` shows what it found. A rule it cannot find falls back to a
safe default and the answer says so.

Policies are not personal data, so this folder is not git-ignored. If yours are confidential, add
`policies/*` (and `!policies/README.md`) to `.gitignore`.
