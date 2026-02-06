sudo systemctl status monday-sincro
sudo systemctl restart cloudflared
journalctl -u monday-sincro -f
