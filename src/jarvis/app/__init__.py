"""Application layer used by the desktop window (and testable without any GUI toolkit): start-up checks, a single-instance lock, background
tasks that can be cancelled and cleaned up, a transcript model, honest feature/model status, redacted diagnostics, and a controller that
calls the EXISTING assistant, memory, mission, scheduler, mode and permission code. No business rules are re-implemented here and nothing here
can grant a permission or skip a confirmation."""
