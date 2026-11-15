// EthOS NAS — terminal default-target resolution
//
// Shared by the Terminal app and the Code Editor's embedded terminal.
//
// The default is always 'host' — a shell as the account running the server:
//  • dev mode: the server runs as the logged-in user, so host *is* that user;
//  • production: the dedicated service account (ethos-nas), which has ACL
//    access to the admin's files (docs/service-user.md).
//
// `user:<name>` runs `su - <name>` and therefore asks for that account's
// password inside the PTY. Automatically starting such a session on every
// login/project open produced an unexpected "Password:" prompt, so switching
// users is an explicit choice in the terminal target picker.

export async function resolveDefaultTerminalTarget() {
    return 'host';
}

