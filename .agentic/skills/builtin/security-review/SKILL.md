# Security Review

Review the DIFF against these classes; cite file/line for every finding:

1. Injection: string-built SQL/shell/HTML; require parameterized queries,
   argv-array process calls, contextual output encoding.
2. Secrets: any credential, token, or key in code, config, tests, or
   logs — including "temporary" ones. Environment names only.
3. AuthN/AuthZ: every new endpoint/route checks identity AND permission;
   no client-supplied trust decisions; sessions invalidated on logout.
4. Input handling: validate type/length/range server-side; path inputs
   canonicalized and confined; uploads restricted by type and size.
5. Crypto: no MD5/SHA1 for security, no homemade primitives, no
   verify=False/TLS bypass, random from a CSPRNG.
6. Data exposure: errors return generic messages; stack traces and
   internal paths never reach clients; logs redact sensitive values.
7. Dependencies: new packages pinned, from the canonical registry, with a
   reason recorded; watch for typosquats.
8. Unsafe deserialization/dynamic execution: pickle/eval/exec/Function on
   external data is a finding, full stop.

Verdict `pass` only with zero medium-or-higher findings.
