#!/usr/bin/env bash
# uptime-watch: small uptime checker for production sites, run by a systemd timer once a minute.
# Config: /etc/uptime-watch/{env,targets}. State: /var/lib/uptime-watch/.
# It only sends GET requests and TLS handshakes to the checked sites, never submits forms.
# env (mode 600): BOT_TOKEN, CHAT_ID for Telegram; MAIL_TO, SMTP_URL, SMTP_USER, SMTP_PASS for email
# (email is off while SMTP_URL is empty). targets: one "type name target" per line.
# Alert after FAILS_TO_ALERT failures in a row, recovery message after the first success.
set -u

CONF_DIR=${CONF_DIR:-/etc/uptime-watch}
STATE_DIR=${STATE_DIR:-/var/lib/uptime-watch}
FAILS_TO_ALERT=${FAILS_TO_ALERT:-3}
CERT_MIN_DAYS=${CERT_MIN_DAYS:-14}
LABEL=${LABEL:-uptime}
BOT_TOKEN= CHAT_ID= MAIL_TO= SMTP_URL= SMTP_USER= SMTP_PASS=
source "$CONF_DIR/env"
mkdir -p "$STATE_DIR"
HOST=$(hostname)

# Cloudflare IPv4 ranges (https://www.cloudflare.com/ips-v4). Proxied A records resolve
# to addresses from these ranges, not to the origin server.
CF_NETS="173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18
108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17
162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22"

send_tg() {
  [[ -n $BOT_TOKEN && -n $CHAT_ID ]] || return 1
  curl -s -m 15 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" --data-urlencode "text=$1" | grep -q '"ok":true'
}

send_mail() {
  [[ -n $SMTP_URL && -n $MAIL_TO ]] || return 1
  printf 'From: %s\r\nTo: %s\r\nSubject: [%s uptime] %s\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n%s\r\n' \
    "$SMTP_USER" "$MAIL_TO" "$LABEL" "${1%%:*}" "$1" |
    curl -s -m 20 --ssl-reqd --url "$SMTP_URL" --user "$SMTP_USER:$SMTP_PASS" \
      --mail-from "$SMTP_USER" --mail-rcpt "$MAIL_TO" --upload-file - >/dev/null
}

# send <text> -> 0 if at least one enabled channel accepted it
send() {
  local ok=1
  send_tg "[$LABEL] $1" && ok=0
  send_mail "$1" && ok=0
  (( ok == 0 )) || echo "send failed (no channel accepted)" >&2
  return $ok
}

fmt_time() { date -d @"$1" '+%H:%M %Z %d.%m'; }

ip2int() { local IFS=.; set -- $1; echo $(( ($1<<24) + ($2<<16) + ($3<<8) + $4 )); }

in_cf() {
  local ip n net bits
  ip=$(ip2int "$1")
  for n in $CF_NETS; do
    net=$(ip2int "${n%/*}"); bits=${n#*/}
    (( (ip >> (32-bits)) == (net >> (32-bits)) )) && return 0
  done
  return 1
}

# check <type> <target> -> prints "ok" or a reason; exit code 0 = ok
check() {
  local code hdrs ips ip end days mx
  case "$1" in
    tcp)
      nc -z -w 6 "${2%:*}" "${2##*:}" >/dev/null 2>&1 && echo ok || echo "port ${2##*:} closed or no answer" ;;
    http)
      # page must return 2xx or 3xx; 4xx/5xx and 000 (no connection) count as failure
      code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$2")
      [[ $code =~ ^[23] ]] && echo ok || echo "HTTP $code" ;;
    render)
      # like http, plus the answer must come from Render (catches a proxy that stopped routing to the app)
      hdrs=$(curl -s -o /dev/null -D - -m 15 "$2")
      # Cloudflare Early Hints sends 103 first, so take the code of the last (final) response
      code=$(sed -n 's/^HTTP[^ ]* \([0-9]*\).*/\1/p' <<<"$hdrs" | tail -n 1)
      # only 103 and no final response = curl timed out after 15 s, not a real status code
      if [[ $code == 1* ]]; then echo "no full response in 15 s (only HTTP $code)"
      elif [[ ! $code =~ ^[23] ]]; then echo "HTTP ${code:-000}"
      elif ! grep -qi '^x-render-origin-server:' <<<"$hdrs"; then echo "HTTP $code, but no x-render-origin-server header (not Render)"
      else echo ok; fi ;;
    cert)
      end=$(echo | timeout 15 openssl s_client -servername "$2" -connect "$2:443" 2>/dev/null |
        openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
      [[ -z $end ]] && { echo "could not get certificate"; return; }
      days=$(( ($(date -d "$end" +%s) - $(date +%s)) / 86400 ))
      (( days >= CERT_MIN_DAYS )) && echo ok || echo "certificate expires in $days days ($end)" ;;
    dnscf)
      # A records via a public resolver; all addresses must be Cloudflare (proxied)
      ips=$(dig +short +time=3 +tries=2 A "$2" @1.1.1.1 | grep -E '^[0-9.]+$')
      [[ -z $ips ]] && ips=$(dig +short +time=3 +tries=2 A "$2" @8.8.8.8 | grep -E '^[0-9.]+$')
      [[ -z $ips ]] && { echo "no A record"; return; }
      for ip in $ips; do in_cf "$ip" || { echo "A $ip is not Cloudflare (proxy off or record changed)"; return; }; done
      echo ok ;;
    mx)
      # target: domain=expected_mx_host
      mx=$(dig +short +time=3 +tries=2 MX "${2%%=*}" @1.1.1.1 | awk '{print $2}' | sed 's/\.$//' | sort | tr '\n' ' ')
      [[ " $mx" == *" ${2#*=} "* ]] && echo ok || echo "MX is now: ${mx:-empty}" ;;
    *) echo "unknown type $1" ;;
  esac
}

fmt_dur() { local s=$1; (( s < 3600 )) && echo "$((s/60)) min" || echo "$((s/3600)) h $((s%3600/60)) min"; }

# If the checking machine itself has no network, stay quiet and keep the counters.
if ! curl -s -m 10 -o /dev/null https://api.telegram.org && ! curl -s -m 10 -o /dev/null https://1.1.1.1; then
  echo "no network on the checking machine, skipping"; exit 0
fi

now=$(date +%s)
while read -r type name target _ <&3; do  # anything after the target is a comment
  [[ -z ${type:-} || $type == \#* ]] && continue
  st="$STATE_DIR/$name"
  fails=0; since=0; alerted=0
  [[ -f $st ]] && read -r fails since alerted < "$st"
  res=$(check "$type" "$target" </dev/null)
  if [[ $res == ok ]]; then
    if (( alerted )); then
      # not sent - keep the state and try again next run
      send "🟢 Back up: $name ($target). Was down $(fmt_dur $((now - since))). [checked from $HOST]" || continue
    fi
    echo "0 0 0" > "$st"
  else
    (( fails == 0 )) && since=$now
    fails=$((fails + 1))
    if (( fails >= FAILS_TO_ALERT && ! alerted )); then
      send "🔴 Down: $name ($target). $res, $fails checks in a row, since $(fmt_time "$since"). [checked from $HOST]" \
        && alerted=1
    fi
    echo "$fails $since $alerted" > "$st"
    echo "FAIL $name $target: $res ($fails)"
  fi
done 3< "$CONF_DIR/targets"
