#!/bin/bash
# Warn via the notifier bot if the cloud VM is unhealthy.
#
# Covers BOTH resources and services. Services were the gap: everything is set to
# restart automatically, so a dead service is unlikely -- but if one stayed down
# the only symptom was the bot going quiet, which is exactly the kind of silent
# failure worth paying for a check.
set -a; . "$HOME/personal-agent/secrets.env"; set +a
CHAT="${TELEGRAM_CHAT_ID}"
MARK=/tmp/vm_watchdog_lastalert
COOLDOWN=43200   # 12h between repeat alerts
DISK_WARN=80
MEM_WARN=90

issues=""

# --- resources ---------------------------------------------------------------
disk=$(df / | awk 'NR==2{gsub("%","",$5); print $5}')
volgb=$(df -BG / | awk 'NR==2{gsub("G","",$2); print $2}')
mem=$(free | awk '/Mem:/{printf "%d", $3/$2*100}')
[ "$disk" -ge "$DISK_WARN" ] && issues="${issues}- Disk ${disk}%% full (of a ${volgb}GB volume)\n"
[ "$mem" -ge "$MEM_WARN" ] && issues="${issues}- Memory ${mem}%% used\n"

# --- systemd services --------------------------------------------------------
for svc in personal-agent-workers personal-agent-llm; do
  if ! systemctl is-active --quiet "$svc"; then
    issues="${issues}- Service ${svc} is NOT running\n"
  fi
done

# --- docker containers -------------------------------------------------------
for c in personal-agent-api-1 personal-agent-n8n-1; do
  if ! docker ps --format '{{.Names}}' | grep -qx "$c"; then
    issues="${issues}- Container ${c} is NOT running\n"
  fi
done

# --- endpoints actually answering (a process can be up but wedged) ------------
check_http() {  # name url
  code=$(curl -s -o /dev/null -w "%{http_code}" -m 10 "$2")
  [ "$code" = "200" ] || issues="${issues}- $1 not responding (HTTP ${code:-timeout})\n"
}
check_http "Reminders API (:8001)" "http://localhost:8001/reminders/list"
check_http "Workers API (:8002)"   "http://localhost:8002/health"
check_http "LLM gateway (:8003)"   "http://localhost:8003/health"
check_http "n8n (:5678)"           "http://localhost:5678/healthz"

# --- every LLM provider down = chat is dead, worth knowing early -------------
avail=$(curl -s -m 10 http://localhost:8003/status \
        | python3 -c "import sys,json;print(sum(1 for p in json.load(sys.stdin)['providers'] if p['available']))" 2>/dev/null)
if [ -n "$avail" ] && [ "$avail" = "0" ]; then
  issues="${issues}- ALL LLM providers are cooling down -- chat will fail until one recovers\n"
fi

# --- alert -------------------------------------------------------------------
if [ -n "$issues" ] && [ -n "$TELEGRAM_NOTIFIER_TOKEN" ]; then
  now=$(date +%s); last=$(cat "$MARK" 2>/dev/null || echo 0)
  if [ $((now-last)) -ge $COOLDOWN ]; then
    text=$(printf "<b>VM Health - $(date '+%a %d %b')</b>\n\nSomething needs attention, master:\n${issues}")
    curl -s -m 15 "https://api.telegram.org/bot${TELEGRAM_NOTIFIER_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${CHAT}" --data-urlencode "parse_mode=HTML" \
      --data-urlencode "text=${text}" >/dev/null
    echo "$now" > "$MARK"
  fi
fi
echo "checked: disk=${disk}%% mem=${mem}%% issues=[${issues:-none}]"
