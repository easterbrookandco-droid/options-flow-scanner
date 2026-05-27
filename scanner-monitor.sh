#!/bin/bash
# scanner-monitor.sh
SESSION="scanner"

# Attach if already exists
if tmux has-session -t $SESSION 2>/dev/null; then
    tmux attach -t $SESSION
    exit 0
fi

# Create session
tmux new-session -d -s $SESSION

# Create 4 panes explicitly:
# Split vertically first (left | right)
tmux split-window -t $SESSION -h

# Split left pane horizontally (top-left | bottom-left)
tmux select-pane -t $SESSION:0.0
tmux split-window -t $SESSION -v

# Split right pane horizontally (top-right | bottom-right)
tmux select-pane -t $SESSION:0.2
tmux split-window -t $SESSION -v

# Assign commands to each pane
# Top-left (pane 0) — Scheduler
tmux select-pane -t $SESSION:0.0
tmux send-keys -t $SESSION:0.0 "sudo journalctl -u scanner-scheduler -f --no-hostname 2>/dev/null" Enter

# Bottom-left (pane 1) — Monitor
tmux send-keys -t $SESSION:0.1 "sudo journalctl -u scanner-monitor -f" Enter

# Top-right (pane 2) — Agent
tmux send-keys -t $SESSION:0.2 "sudo journalctl -u scanner-agent -f" Enter

# Bottom-right (pane 3) — Status
tmux send-keys -t $SESSION:0.3 "watch -n 30 'sudo systemctl status scanner-scheduler scanner-monitor scanner-agent --no-pager | grep -E \"Active|scanner\"'" Enter

# Focus top-left
tmux select-pane -t $SESSION:0.0

# Attach
tmux attach -t $SESSION#!/bin/bash
# scanner-monitor.sh
# Launches a 4-pane tmux session showing all scanner logs
# Usage: bash ~/scanner-monitor.sh
# Reattach later: tmux attach -t scanner

SESSION="scanner"

# If session already exists, just attach to it
if tmux has-session -t $SESSION 2>/dev/null; then
    echo "Session '$SESSION' already exists — attaching..."
    tmux attach -t $SESSION
    exit 0
fi

# Create new session with scheduler log in first pane
tmux new-session -d -s $SESSION -x 220 -y 50

# Top-left: Scheduler
tmux send-keys -t $SESSION "sudo journalctl -u scanner-scheduler -f" Enter

# Split top into left/right — Agent goes top-right
tmux split-window -t $SESSION -h
tmux send-keys -t $SESSION "sudo journalctl -u scanner-agent -f" Enter

# Split bottom half — Monitor goes bottom-left
tmux select-pane -t $SESSION:0.0
tmux split-window -t $SESSION -v
tmux send-keys -t $SESSION "sudo journalctl -u scanner-monitor -f" Enter

# Split bottom-right — Status overview
tmux split-window -t $SESSION -h
tmux send-keys -t $SESSION "watch -n 30 'sudo systemctl status scanner-scheduler scanner-monitor scanner-agent scanner-dashboard --no-pager | grep -E \"Active|Main PID|scanner\"'" Enter

# Add pane labels (optional — tmux 3.0+)
tmux select-pane -t $SESSION:0.0 -T "SCHEDULER"
tmux select-pane -t $SESSION:0.1 -T "MONITOR"
tmux select-pane -t $SESSION:0.2 -T "AGENT"
tmux select-pane -t $SESSION:0.3 -T "STATUS"

# Focus top-left pane
tmux select-pane -t $SESSION:0.0

# Attach
tmux attach -t $SESSION
