# nginx on the validator host (.88)

Beyond the per-site files (api/control/validator, `model-addresses/`):

- `/etc/nginx/conf.d/gc-gzip-json.conf` — this directory's `gc-gzip-json.conf`:
  gzip the API's JSON (SSE streams stay uncompressed).
- `/etc/nginx/conf.d/gc-server-names.conf` — `../model-addresses/gc-server-names.conf`:
  room for the long model-address host names.
- `/etc/nginx/nginx.conf`: `worker_processes 16;` and `worker_rlimit_nofile 8192;`
  instead of `worker_processes auto;`. The host has 256 cores, so `auto` started
  256 workers and every reload failed with "Too many open files" while old and
  new workers overlapped (the model-address certificate job reloads nginx).

Always `nginx -t` before `systemctl reload nginx`.
