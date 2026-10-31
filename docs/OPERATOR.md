# Computer Operator

Jarvis can operate your Windows PC through a **fixed list of named capabilities** (see `CAPABILITY_MATRIX.md` for the full, honest list and what is *not* done).
It is **off until you turn it on** on the Operator page. Nothing in it runs arbitrary commands.

## Safety model
1. **Master switch** (Operator page). Off = every request answers "Computer control is off". Guest sessions can use nothing here.
2. **Permissions** by category (files, windows, power, backup, settings, services, ...). Turning the switch on grants only reading/everyday categories; changes, power, services, game mode, etc. are granted one by one.
3. **Folders**: file tools only work in Desktop, Documents, Downloads, Pictures, Videos, Music and folders you add. Whole drives, your whole user folder and system folders cannot be added.
4. **Risk and confirmation**: LOW runs; MEDIUM asks (you can switch that off); HIGH (delete, close/force-close, power, install-like, git commit/push, test runs) **always asks right before it runs**.
5. **Emergency stop**: the STOP file, Pause and Cancel on the Operator page always win. Workflows check them between steps.
6. **Verification**: after acting, Jarvis looks again where it can (window minimized? file there? hash equal?) and says *verified*, *unverified* (could not be checked) or *failed*. It never claims success it did not check.
7. **Audit log** (`operator_audit.jsonl` in your profile): what was asked, decision and result. No file contents, passwords or keys are written.
8. **Secrets**: if you type something that looks like a password or API key into chat, Jarvis masks it before it is stored and tells you.

## How to use
Type or say whole sentences such as "lock my pc", "how much RAM am I using", "find duplicate files in Downloads", "organize my downloads" (shows a plan first; apply and undo are separate), "back up my Documents folder", "snap discord to the left", "start game mode". Anything that is not clearly a computer request goes to the normal assistant.

Files: organizing never overwrites, never deletes; delete goes to a Jarvis recovery folder and can be restored; ZIP extraction refuses unsafe paths and never overwrites; backups are verified with SHA-256 and restore into a new folder.

Workflows: save a list of capabilities, run it, or schedule it (every N minutes / daily at / on events such as an app opening or a USB drive appearing). Each step goes through the same permissions and confirmations. A workflow someone else gave you is imported **paused and untrusted** and asks before every run.

## What it does not do
Browser driving or form filling, installing/uninstalling software, changing network or security settings, entering passwords or payments, administrator rights, shell/PowerShell/registry commands, reading mailboxes, camera. See the matrix.

## Verification status
Automated tests use real temporary files and a **fake Windows layer**. The Windows-only parts (window control, power, volume, PowerShell queries, services, Defender/firewall read-outs, settings pages) have **not been run on a real Windows PC**. Test them there first, starting with read-only actions.
