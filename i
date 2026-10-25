#!/bin/sh
# Короткая ссылка на установку, чтобы набрать её руками:
#   curl -L raw.githubusercontent.com/mokspp-alt/personal-assistant-bot/main/i | sh
set -e
curl -fsSL https://raw.githubusercontent.com/mokspp-alt/personal-assistant-bot/main/deploy/install.sh -o /tmp/personal-assistant-bot-install.sh
bash /tmp/personal-assistant-bot-install.sh
