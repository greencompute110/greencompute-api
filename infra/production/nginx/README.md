# nginx on the validator host (.88)

Beyond the per-site files (api/control/validator, `model-addresses/`):

- `/etc/nginx/conf.d/gc-gzip-json.conf` — this directory's `gc-gzip-json.conf`:
  gzip the API's JSON (SSE streams stay uncompressed).
- `/etc/nginx/nginx.conf`, in the `http` block: `server_names_hash_bucket_size 128;`
  (room for the long model-address host names). It must be in nginx.conf
  itself, not conf.d: certbot's nginx plugin adds its own copy to nginx.conf's
  `http` block unless one is there already, and a second copy in conf.d makes
  its reload fail ("directive is duplicate"). That silently broke every
  api/control/validator renewal from 2026-10-21 until the certificate expired
  on 2026-11-20; the gateway's TLS watch (application/tls_watch.py) now alerts
  14 days before any of our certificates expires.
- `/etc/nginx/nginx.conf`: `worker_processes 16;` and `worker_rlimit_nofile 8192;`
  instead of `worker_processes auto;`. The host has 256 cores, so `auto` started
  256 workers and every reload failed with "Too many open files" while old and
  new workers overlapped (the model-address certificate job reloads nginx).

Always `nginx -t` before `systemctl reload nginx`.
