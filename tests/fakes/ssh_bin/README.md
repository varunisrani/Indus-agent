# Fake `ssh` (P24-30)

`ssh` here is a POSIX shell script written for Indus that stands in for OpenSSH in the desktop's
SSH tests (`apps/desktop/electron/connections/ssh.test.ts`, `sshUpdate.test.ts`). It never opens a
network connection: the "remote" is the machine running the tests and a remote command runs with
`sh -c`. Scenarios and the call log are chosen with the environment variables listed at the top of
the script. The host-key banner it prints imitates the shape of OpenSSH's message so the client's
error classification can be tested; it is not copied from any other project.
