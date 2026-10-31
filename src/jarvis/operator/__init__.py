"""Computer Operator: Jarvis operating the Windows PC through a FIXED list of named capabilities.

* Off until you turn it on (Operator page); every capability belongs to a permission category you grant one by one.
* Low-risk actions run, medium ones ask first (switchable), high-risk ones ALWAYS ask immediately before they run.
* Every action is written to a separate audit log (no passwords or file contents) and, where Jarvis can check, the result is verified.
* The emergency stop (the STOP file), pause and cancel always win. A guest session can use nothing here.
* Nothing in this package runs arbitrary commands: external programs are only started from fixed argument lists built in this package.
Typed and spoken commands go through exactly the same gate (operator.commands)."""
