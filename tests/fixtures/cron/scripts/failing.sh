# Failing fixture (P14-09): a message on stderr and exit code 3.
echo "partial output"
echo "the upstream service refused the request" >&2
exit 3
