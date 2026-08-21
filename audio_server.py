#!/usr/bin/env python3
"""
Multi-Channel Meeting Audio Simulator & Browser Server
Usage:
    python3 audio_server.py [--host HOST] [--port PORT] [--dir INITIAL_DIR]

Default:
    Listens on 0.0.0.0:8000
    Default browsing path: /data/vmfs/main01a_shared/Download/NOTSOFAR-1/
"""

import argparse
import html
import json
import mimetypes
import os
import socketserver
import sys
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Additional audio mimetypes registration
mimetypes.add_type('audio/wav', '.wav')
mimetypes.add_type('audio/mpeg', '.mp3')
mimetypes.add_type('audio/flac', '.flac')
mimetypes.add_type('audio/ogg', '.ogg')
mimetypes.add_type('audio/aac', '.aac')
mimetypes.add_type('audio/mp4', '.m4a')
mimetypes.add_type('audio/webm', '.webm')
mimetypes.add_type('audio/opus', '.opus')

# .wma intentionally excluded: no mainstream browser <audio> element can decode it, and it was
# never registered with mimetypes.add_type() above, so serving one falls back to a bogus
# audio/mpeg Content-Type on top of failing to play regardless.
AUDIO_EXTENSIONS = {'.wav', '.mp3', '.flac', '.ogg', '.aac', '.m4a', '.webm', '.opus'}

DEFAULT_DIR = "/data/vmfs/main01a_shared/Download/NOTSOFAR-1/"

def format_size(num_bytes):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🎙️ Multi-Channel Meeting Audio Simulator</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #0f172a;
            --panel-bg: #1e293b;
            --panel-border: #334155;
            --accent-blue: #38bdf8;
            --accent-indigo: #6366f1;
            --accent-green: #10b981;
            --accent-purple: #a855f7;
            --accent-pink: #ec4899;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --hover-bg: #334155;
            --active-bg: #475569;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--bg-dark);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }

        header {
            background: linear-gradient(135deg, #1e1b4b 0%, #0f172a 100%);
            border-bottom: 1px solid var(--panel-border);
            padding: 1.25rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }

        .logo-title { display: flex; align-items: center; gap: 0.75rem; }

        .logo-icon {
            font-size: 1.75rem;
            background: rgba(99, 102, 241, 0.2);
            padding: 0.5rem;
            border-radius: 12px;
            border: 1px solid rgba(99, 102, 241, 0.4);
        }

        h1 {
            font-size: 1.35rem;
            font-weight: 700;
            background: linear-gradient(to right, #38bdf8, #818cf8, #c084fc);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .header-actions { display: flex; align-items: center; gap: 0.75rem; }

        .badge {
            background: rgba(16, 185, 129, 0.15);
            color: var(--accent-green);
            border: 1px solid rgba(16, 185, 129, 0.3);
            padding: 0.35rem 0.85rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 0.4rem;
        }

        .pulse-dot {
            width: 8px;
            height: 8px;
            background-color: var(--accent-green);
            border-radius: 50%;
            animation: pulse 1.8s infinite;
        }

        @keyframes pulse {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { transform: scale(1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }

        .container {
            max-width: 1400px;
            width: 100%;
            margin: 0 auto;
            padding: 1.5rem;
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
        }

        .secure-warning {
            display: none;
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid rgba(239, 68, 68, 0.4);
            color: #fca5a5;
            padding: 0.85rem 1.25rem;
            border-radius: 12px;
            font-size: 0.85rem;
            line-height: 1.5;
        }

        .secure-warning strong { color: #ffffff; }

        .path-bar {
            background-color: var(--panel-bg);
            border: 1px solid var(--panel-border);
            border-radius: 12px;
            padding: 0.85rem 1.25rem;
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .path-bar label {
            font-weight: 600;
            color: var(--text-muted);
            font-size: 0.85rem;
            white-space: nowrap;
        }

        .path-input-group { display: flex; flex: 1; gap: 0.5rem; }

        input[type="text"], select {
            background: #0f172a;
            border: 1px solid var(--panel-border);
            color: var(--text-main);
            padding: 0.6rem 1rem;
            border-radius: 8px;
            font-family: 'Inter', sans-serif;
            font-size: 0.9rem;
            outline: none;
            transition: border-color 0.2s;
        }

        input[type="text"]:focus, select:focus { border-color: var(--accent-indigo); }

        .btn {
            background-color: var(--accent-indigo);
            color: white;
            border: none;
            padding: 0.6rem 1.2rem;
            border-radius: 8px;
            font-weight: 600;
            font-size: 0.85rem;
            cursor: pointer;
            transition: all 0.2s;
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            white-space: nowrap;
        }

        .btn:hover { opacity: 0.9; transform: translateY(-1px); }

        .btn-secondary {
            background-color: var(--hover-bg);
            color: var(--text-main);
            border: 1px solid var(--panel-border);
        }

        .btn-secondary:hover { background-color: var(--active-bg); }
        .btn-success { background-color: var(--accent-green); color: #0f172a; }

        .breadcrumbs {
            display: flex;
            align-items: center;
            gap: 0.4rem;
            flex-wrap: wrap;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.85rem;
            padding: 0.2rem 0;
        }

        .crumb-item {
            color: var(--accent-blue);
            cursor: pointer;
            padding: 0.2rem 0.4rem;
            border-radius: 4px;
        }

        .crumb-item:hover { background-color: var(--hover-bg); text-decoration: underline; }
        .crumb-separator { color: var(--text-muted); }

        .master-bar {
            background: linear-gradient(135deg, rgba(30, 41, 59, 0.9) 0%, rgba(15, 23, 42, 0.9) 100%);
            border: 1px solid var(--accent-indigo);
            border-radius: 14px;
            padding: 1rem 1.5rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 1rem;
        }

        .master-title {
            display: flex;
            align-items: center;
            gap: 0.6rem;
            font-weight: 700;
            font-size: 1.05rem;
            color: var(--accent-blue);
        }

        .master-controls {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .channels-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
            gap: 1.25rem;
        }

        .channel-card {
            background: linear-gradient(145deg, #1e293b 0%, #0f172a 100%);
            border: 1px solid var(--panel-border);
            border-radius: 16px;
            padding: 1.25rem;
            display: flex;
            flex-direction: column;
            gap: 1rem;
            position: relative;
            box-shadow: 0 4px 15px rgba(0,0,0,0.2);
        }

        .channel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--panel-border);
            padding-bottom: 0.75rem;
        }

        .channel-tag {
            font-size: 0.85rem;
            font-weight: 700;
            padding: 0.25rem 0.6rem;
            border-radius: 6px;
            display: flex;
            align-items: center;
            gap: 0.4rem;
        }

        .channel-tag-1 { background: rgba(56, 189, 248, 0.15); color: var(--accent-blue); border: 1px solid rgba(56, 189, 248, 0.3); }
        .channel-tag-2 { background: rgba(168, 85, 247, 0.15); color: var(--accent-purple); border: 1px solid rgba(168, 85, 247, 0.3); }
        .channel-tag-3 { background: rgba(236, 72, 153, 0.15); color: var(--accent-pink); border: 1px solid rgba(236, 72, 153, 0.3); }

        .device-selector-group {
            display: flex;
            flex-direction: column;
            gap: 0.4rem;
        }

        .device-selector-group label {
            font-size: 0.75rem;
            font-weight: 600;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .device-select {
            width: 100%;
            font-size: 0.85rem;
        }

        .track-info-box {
            background: rgba(15, 23, 42, 0.7);
            border: 1px dashed var(--panel-border);
            border-radius: 10px;
            padding: 0.75rem 1rem;
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
        }

        .track-name {
            font-weight: 600;
            font-size: 0.95rem;
            color: var(--text-main);
            word-break: break-all;
        }

        .track-path-sub {
            font-size: 0.75rem;
            color: var(--text-muted);
            font-family: 'JetBrains Mono', monospace;
            word-break: break-all;
        }

        .audio-wrapper {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }

        audio { width: 100%; height: 42px; border-radius: 8px; outline: none; }

        .channel-controls-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 0.8rem;
            color: var(--text-muted);
        }

        .checkbox-label {
            display: flex;
            align-items: center;
            gap: 0.4rem;
            cursor: pointer;
        }

        .level-meter-row {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.75rem;
            color: var(--text-muted);
        }

        .level-meter-track {
            flex: 1;
            height: 10px;
            background: rgba(15, 23, 42, 0.7);
            border: 1px solid var(--panel-border);
            border-radius: 999px;
            overflow: hidden;
        }

        .level-meter-fill {
            height: 100%;
            width: 0%;
            background: var(--accent-green);
            border-radius: 999px;
            transition: width 80ms linear, background-color 80ms linear;
        }

        .content-card {
            background-color: var(--panel-bg);
            border: 1px solid var(--panel-border);
            border-radius: 16px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            flex: 1;
        }

        .card-toolbar {
            padding: 1rem 1.25rem;
            background-color: rgba(15, 23, 42, 0.4);
            border-bottom: 1px solid var(--panel-border);
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 1rem;
        }

        .search-box { max-width: 300px; width: 100%; }
        .file-count { font-size: 0.85rem; color: var(--text-muted); }
        .file-table-wrapper { overflow-x: auto; flex: 1; }

        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.9rem; }

        th {
            background-color: rgba(15, 23, 42, 0.7);
            color: var(--text-muted);
            padding: 0.85rem 1.25rem;
            font-weight: 600;
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            border-bottom: 1px solid var(--panel-border);
        }

        td {
            padding: 0.85rem 1.25rem;
            border-bottom: 1px solid rgba(51, 65, 85, 0.5);
            vertical-align: middle;
        }

        tr:hover td { background-color: var(--hover-bg); }

        .item-name {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            font-weight: 500;
            color: var(--text-main);
            cursor: pointer;
        }

        .item-name:hover { color: var(--accent-blue); }
        .item-icon { font-size: 1.2rem; width: 24px; text-align: center; }

        .size-col {
            color: var(--text-muted);
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.85rem;
            width: 120px;
        }

        .action-col { width: 260px; text-align: right; }

        .btn-assign {
            padding: 0.35rem 0.65rem;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 600;
            cursor: pointer;
            border: 1px solid transparent;
            transition: all 0.2s;
            margin-left: 0.3rem;
        }

        .btn-assign-ch1 { background-color: rgba(56, 189, 248, 0.15); color: var(--accent-blue); border-color: rgba(56, 189, 248, 0.3); }
        .btn-assign-ch1:hover { background-color: var(--accent-blue); color: #0f172a; }

        .btn-assign-ch2 { background-color: rgba(168, 85, 247, 0.15); color: var(--accent-purple); border-color: rgba(168, 85, 247, 0.3); }
        .btn-assign-ch2:hover { background-color: var(--accent-purple); color: white; }

        .empty-state { padding: 3rem; text-align: center; color: var(--text-muted); }

        footer {
            text-align: center;
            padding: 1rem;
            font-size: 0.8rem;
            color: var(--text-muted);
            border-top: 1px solid var(--panel-border);
        }
    </style>
</head>
<body>
    <header>
        <div class="logo-title">
            <div class="logo-icon">🎧</div>
            <div>
                <h1>Multi-Channel Meeting Audio Simulator</h1>
                <div style="font-size: 0.75rem; color: var(--text-muted);">Simulate multi-party meetings with target sound devices (BlackHole)</div>
            </div>
        </div>
        <div class="header-actions">
            <button class="btn btn-secondary" id="btn-detect-devices">🎤 Unblock macOS Device Names</button>
            <div class="badge">
                <div class="pulse-dot"></div>
                <span id="device-count-badge">Devices Ready</span>
            </div>
        </div>
    </header>

    <div class="container">
        <!-- Secure Context Warning Banner -->
        <div class="secure-warning" id="secure-warning-box">
            <strong>⚠️ Security Notice (Insecure Context):</strong> Web Audio device enumeration and <code>setSinkId</code> require a <strong>Secure Context</strong> (HTTPS or <code>http://localhost:8000</code>).<br>
            If accessing via plain HTTP IP, open via HTTPS reverse proxy (e.g. <code>https://my-meeting-notes.dev.local.shifamily.com/simulator</code>) or SSH tunnel <code>ssh -L 8000:localhost:8000</code>.
        </div>

        <!-- Path Navigation -->
        <div class="path-bar">
            <label for="current-path-input">Directory:</label>
            <div class="path-input-group">
                <input type="text" id="current-path-input" value="" placeholder="/path/to/audio/files">
                <button class="btn btn-secondary" id="btn-up" title="Parent Directory">⬆️ Up</button>
                <button class="btn" id="btn-go">Go</button>
            </div>
        </div>
        <div class="breadcrumbs" id="breadcrumbs"></div>

        <!-- Master Sync Controls -->
        <div class="master-bar">
            <div class="master-title">
                <span>⚡ Multi-Channel Master Sync</span>
            </div>
            <div class="master-controls">
                <button class="btn btn-success" id="btn-master-play">▶ Play All Channels</button>
                <button class="btn btn-secondary" id="btn-master-pause">⏸ Pause All</button>
                <button class="btn btn-secondary" id="btn-master-restart">⏮ Sync Restart</button>
                <button class="btn btn-secondary" id="btn-add-channel">+ Add Channel</button>
            </div>
        </div>

        <!-- Channels Grid -->
        <div class="channels-grid" id="channels-container"></div>

        <!-- Directory Content List -->
        <div class="content-card">
            <div class="card-toolbar">
                <div class="search-box">
                    <input type="text" id="file-filter" placeholder="Filter audio files (.wav, speaker_01)...">
                </div>
                <div class="file-count" id="file-count-label">0 items</div>
            </div>
            <div class="file-table-wrapper">
                <table>
                    <thead>
                        <tr>
                            <th>Name</th>
                            <th class="size-col">Size</th>
                            <th class="action-col">Route to Channel</th>
                        </tr>
                    </thead>
                    <tbody id="file-list-body">
                        <tr><td colspan="3" class="empty-state">Loading directory contents...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <footer>
        Meeting Audio Simulator &bull; Binding macOS Sound Devices (BlackHole, Speakers, Headphones) &bull; 0.0.0.0
    </footer>

    <script>
        const INITIAL_PATH = "__INITIAL_PATH__";
        let currentDir = "";
        let audioOutputDevices = [];

        // Determine base API path relative to current URL path (supports / or /simulator/ reverse proxy)
        const getApiUrl = (endpoint, params = {}) => {
            let basePath = window.location.pathname;
            if (!basePath.endsWith('/')) {
                basePath += '/';
            }
            const url = new URL(basePath + endpoint, window.location.origin);
            Object.keys(params).forEach(key => url.searchParams.append(key, params[key]));
            return url.toString();
        };

        // Check Secure Context
        const isSecure = window.isSecureContext || window.location.protocol === 'https:' || window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
        const secureWarningBox = document.getElementById('secure-warning-box');

        if (!isSecure) {
            secureWarningBox.style.display = 'block';
        }

        // Dynamic Channels State. `separateTab`/`loop` are bookkeeping for a
        // channel whose actual <audio> element lives in another tab (see
        // toggleSeparateTab) -- this page never touches that element
        // directly, only relays commands to it over BroadcastChannel, so its
        // current loop/device/track state has to be remembered here too in
        // order to replay it at a freshly (re)opened tab.
        let channels = [
            {
                id: 1,
                name: "Channel 1 (Room Discussion)",
                tagClass: "channel-tag-1",
                trackTitle: "No Audio Assigned",
                trackPath: "Assign a room recording file below...",
                deviceId: "default",
                separateTab: false,
                loop: false
            },
            {
                id: 2,
                name: "Channel 2 (My Talk / Mic Simulator)",
                tagClass: "channel-tag-2",
                trackTitle: "No Audio Assigned",
                trackPath: "Assign your talk recording file below...",
                deviceId: "default",
                separateTab: false,
                loop: false
            }
        ];

        // Cross-tab control: every channel-player tab (CHANNEL_HTML_TEMPLATE)
        // and this master page share one BroadcastChannel. See that
        // template's module-level doc comment in audio_server.py for why
        // separate tabs exist at all -- this is just the plumbing that keeps
        // "one master, N tabs" feeling like one page.
        const simControl = new BroadcastChannel('meeting-sim-control');
        // Tracks window handles opened via openChannelTab, purely so a
        // repeat click can .focus() an already-open tab instead of asking
        // the browser to spawn a duplicate -- window.open() with the same
        // target name already does this on its own, but keeping the handle
        // lets us skip even asking when we already have a live reference.
        const channelWindows = {};

        const pathInput = document.getElementById('current-path-input');
        const btnGo = document.getElementById('btn-go');
        const btnUp = document.getElementById('btn-up');
        const breadcrumbsEl = document.getElementById('breadcrumbs');
        const fileListBody = document.getElementById('file-list-body');
        const filterInput = document.getElementById('file-filter');
        const fileCountLabel = document.getElementById('file-count-label');
        const channelsContainer = document.getElementById('channels-container');
        const btnDetectDevices = document.getElementById('btn-detect-devices');
        const deviceCountBadge = document.getElementById('device-count-badge');

        const btnMasterPlay = document.getElementById('btn-master-play');
        const btnMasterPause = document.getElementById('btn-master-pause');
        const btnMasterRestart = document.getElementById('btn-master-restart');
        const btnAddChannel = document.getElementById('btn-add-channel');

        // Safely Request Audio Permissions
        btnDetectDevices.onclick = async () => {
            if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
                alert("Web Media Devices API is unavailable on this plain HTTP origin.\\n\\nPlease open via HTTPS (e.g. https://my-meeting-notes.dev.local.shifamily.com/simulator) or http://localhost:8000.");
                return;
            }

            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                stream.getTracks().forEach(t => t.stop());
                await enumerateDevices();
                alert("macOS Audio Device Labels unblocked! Check channel dropdowns for BlackHole / Speakers.");
            } catch (err) {
                alert("Audio device permission denied or unavailable: " + err.message);
            }
        };

        async function enumerateDevices() {
            if (!navigator.mediaDevices || typeof navigator.mediaDevices.enumerateDevices !== 'function') {
                deviceCountBadge.textContent = "Requires HTTPS/localhost";
                return;
            }

            try {
                const devices = await navigator.mediaDevices.enumerateDevices();
                audioOutputDevices = devices.filter(d => d.kind === 'audiooutput');
                deviceCountBadge.textContent = `${audioOutputDevices.length} Output Device(s)`;
                updateAllDeviceDropdowns();
            } catch (err) {
                console.error("Failed to enumerate devices:", err);
            }
        }

        function updateAllDeviceDropdowns() {
            channels.forEach(ch => {
                const selectEl = document.getElementById(`ch-device-select-${ch.id}`);
                if (!selectEl) return;

                const currentVal = selectEl.value;
                selectEl.innerHTML = `<option value="default">Default macOS Output</option>`;

                audioOutputDevices.forEach(d => {
                    if (d.deviceId === 'default') return;
                    const opt = document.createElement('option');
                    opt.value = d.deviceId;
                    opt.textContent = d.label || `Device (${d.deviceId.substring(0, 8)}...)`;
                    selectEl.appendChild(opt);
                });

                selectEl.value = currentVal;
            });
        }

        // Builds the URL a separate-tab channel opens: the same index route
        // this page is already on (so a reverse-proxy path prefix like
        // /simulator/ carries through automatically), with ?channel=&name=
        // for CHANNEL_HTML_TEMPLATE's script to read via URLSearchParams.
        // Query params, not a distinct path -- there's nothing server-side
        // that needs to know the channel id, only the client script.
        function getChannelTabUrl(ch) {
            let basePath = window.location.pathname;
            if (!basePath.endsWith('/')) basePath += '/';
            const url = new URL(basePath, window.location.origin);
            url.searchParams.set('channel', ch.id);
            url.searchParams.set('name', ch.name);
            return url.toString();
        }

        function openChannelTab(ch) {
            // A stable per-channel window name is what makes a second click
            // focus the existing tab instead of opening a duplicate -- this
            // is a browser-level guarantee (same name + still open = same
            // browsing context), the channelWindows handle below is just an
            // optimization on top of it.
            const win = window.open(getChannelTabUrl(ch), `meeting-sim-channel-${ch.id}`);
            if (win) {
                channelWindows[ch.id] = win;
                win.focus();
            }
            return win;
        }

        // Pushes this channel's full current state at whichever tab is
        // listening. Called both when the master actively changes something
        // (a new file assigned, the tab was just opened) and in response to
        // that tab's own 'hello' handshake -- BroadcastChannel delivers to
        // whoever happens to be subscribed *right now*, with no queue or
        // replay, so a tab that opens (or reopens) after the fact needs
        // this resent or it just sits there blank.
        function sendLoad(ch) {
            if (!ch.trackPath || !ch.trackPath.startsWith('/')) return; // still the placeholder text
            simControl.postMessage({
                kind: 'load', channelId: ch.id, path: ch.trackPath, name: ch.trackTitle,
                deviceId: ch.deviceId, loop: !!ch.loop,
            });
        }

        function sendPlay(chId) { simControl.postMessage({ kind: 'play', channelId: chId }); }
        function sendPause(chId) { simControl.postMessage({ kind: 'pause', channelId: chId }); }
        function sendRestart(chId) { simControl.postMessage({ kind: 'restart', channelId: chId }); }

        function toggleSeparateTab(chId, isChecked) {
            const ch = channels.find(c => c.id === chId);
            if (!ch) return;
            ch.separateTab = isChecked;
            renderChannels();
            if (isChecked) {
                openChannelTab(ch);
                // The freshly opened tab's own 'hello' handshake covers the
                // steady-state case, but the very first open can race its
                // BroadcastChannel subscription being ready -- this is a
                // belt-and-braces resend, not the only path that fires.
                setTimeout(() => sendLoad(ch), 300);
            } else if (ch.trackPath && ch.trackPath.startsWith('/')) {
                // renderChannels() just built a brand new, src-less <audio>
                // for the local-player card it switched back to -- without
                // this, the title/path still show the previously-assigned
                // track (ch.trackTitle/trackPath never changed) but it
                // silently won't play until re-picked from the file table.
                const audioEl = document.getElementById(`ch-audio-${chId}`);
                if (audioEl) {
                    audioEl.src = getApiUrl('api/stream', { path: ch.trackPath });
                    audioEl.loop = !!ch.loop;
                    if (ch.deviceId && typeof audioEl.setSinkId === 'function') {
                        audioEl.setSinkId(ch.deviceId).catch(console.error);
                    }
                }
            }
            // Deliberately does not close the tab on uncheck -- the user may
            // still want it (e.g. to compare against the merged-back local
            // playback), and closing a tab out from under someone without
            // asking is its own kind of surprising data loss.
        }

        // Relays from a channel-player tab: it owns no DOM of its own here,
        // so its status line, level meter and remembered device selection
        // all arrive as messages rather than being read off a local <audio>
        // element the way a non-separate channel's do.
        simControl.onmessage = (ev) => {
            const msg = ev.data;
            if (!msg || typeof msg.channelId !== 'number') return;
            const ch = channels.find(c => c.id === msg.channelId);
            if (!ch) return;

            switch (msg.kind) {
                case 'hello':
                    if (ch.trackPath) sendLoad(ch);
                    break;
                case 'status': {
                    const statusEl = document.getElementById(`ch-status-${ch.id}`);
                    if (statusEl) {
                        statusEl.textContent = msg.text || '';
                        statusEl.style.color = msg.color || '';
                    }
                    break;
                }
                case 'level': {
                    const fillEl = document.getElementById(`ch-level-fill-${ch.id}`);
                    const trackEl = document.getElementById(`ch-level-track-${ch.id}`);
                    if (fillEl) {
                        fillEl.style.width = `${msg.pct}%`;
                        fillEl.style.backgroundColor =
                            msg.pct > 85 ? '#ef4444' : msg.pct > 60 ? '#f59e0b' : 'var(--accent-green)';
                    }
                    if (trackEl) trackEl.setAttribute('aria-valuenow', String(msg.pct));
                    break;
                }
                case 'deviceChanged':
                    // Bookkeeping only, so a later sendLoad (reopen, hello
                    // resend) tells the tab to reselect whatever it was last
                    // set to -- the master itself never calls setSinkId for
                    // a separate-tab channel.
                    ch.deviceId = msg.deviceId;
                    break;
            }
        };

        // Per-channel level meters: a silent WebAudio tap on a *copy* of
        // each <audio> element's decoded output (captureStream()), never
        // wired onward to ctx.destination. A MediaElementAudioSourceNode
        // straight off the element instead would hijack its entire audio
        // path through the AudioContext's own destination device, silently
        // undoing setSinkId() -- the whole point of this page is routing
        // each channel to a specific macOS device, so this taps a copy
        // rather than the real signal path. Same "tap, don't hijack"
        // principle as this app's own web/src/hooks/useRecorder.ts.
        //
        // Seeing both channels' meters move together (or one channel's
        // level tracking audio it was never assigned) is itself a useful
        // diagnostic -- it means whatever's driving that flash is arriving
        // before this page's own audio graph, i.e. real device-level bleed
        // (e.g. a macOS Aggregate/Multi-Output Device combining BlackHole
        // with a real output), not a bug in this file.
        const channelMeters = {};

        function stopLevelMeter(chId) {
            const meter = channelMeters[chId];
            if (!meter) return;
            if (meter.raf) cancelAnimationFrame(meter.raf);
            try { meter.source.disconnect(); } catch (err) { /* already gone */ }
            try { meter.analyser.disconnect(); } catch (err) { /* already gone */ }
            meter.ctx.close().catch(() => {});
            delete channelMeters[chId];
        }

        function startLevelMeter(ch, audioEl) {
            // One tap per element for its whole lifetime -- the 'play' event
            // fires again on every resume, restart and loop iteration, and
            // stacking up duplicate taps would both leak AudioContexts and
            // make the meter read hotter each time.
            if (audioEl.dataset.meterAttached === 'true') return;
            if (typeof audioEl.captureStream !== 'function') {
                console.warn(`Channel ${ch.id}: captureStream() unsupported in this browser, no level meter.`);
                return;
            }

            let stream;
            try {
                stream = audioEl.captureStream();
            } catch (err) {
                console.warn(`Channel ${ch.id}: captureStream() failed:`, err);
                return;
            }
            if (stream.getAudioTracks().length === 0) return;

            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            void ctx.resume();
            const source = ctx.createMediaStreamSource(stream);
            const analyser = ctx.createAnalyser();
            analyser.fftSize = 1024;
            source.connect(analyser); // deliberately no .connect(ctx.destination) -- see above

            audioEl.dataset.meterAttached = 'true';
            channelMeters[ch.id] = { ctx, source, analyser, raf: null };

            const fillEl = document.getElementById(`ch-level-fill-${ch.id}`);
            const trackEl = document.getElementById(`ch-level-track-${ch.id}`);
            const samples = new Uint8Array(analyser.fftSize);
            let level = 0;

            function tick() {
                if (!channelMeters[ch.id]) return; // stopped from under us
                analyser.getByteTimeDomainData(samples);
                // Peak deviation from silence (128), the same measure and
                // decay curve as the main app's recorder level meter, so a
                // side-by-side comparison means the same thing in both UIs.
                let peak = 0;
                for (let i = 0; i < samples.length; i++) {
                    const dev = Math.abs(samples[i] - 128);
                    if (dev > peak) peak = dev;
                }
                const now = Math.min(1, peak / 128);
                level = now > level ? now : level * 0.85;

                if (fillEl) {
                    const pct = Math.round(level * 100);
                    fillEl.style.width = `${pct}%`;
                    fillEl.style.backgroundColor =
                        pct > 85 ? '#ef4444' : pct > 60 ? '#f59e0b' : 'var(--accent-green)';
                    if (trackEl) trackEl.setAttribute('aria-valuenow', String(pct));
                }
                channelMeters[ch.id].raf = requestAnimationFrame(tick);
            }
            tick();

            // The tap dies with its track -- covers the element being
            // removed by renderChannels() (see its cleanup call below)
            // without a second cleanup path to keep in sync.
            stream.getAudioTracks()[0].addEventListener('ended', () => stopLevelMeter(ch.id));
        }

        function renderChannels() {
            // Every rendered <audio> element is about to be discarded and
            // recreated from scratch (innerHTML wipe below) -- any meter
            // still tapping one of them would otherwise leak its
            // AudioContext and keep animating a detached DOM node.
            Object.keys(channelMeters).forEach(id => stopLevelMeter(Number(id)));
            channelsContainer.innerHTML = "";
            channels.forEach(ch => {
                const card = document.createElement('div');
                card.className = "channel-card active-channel";
                card.id = `channel-card-${ch.id}`;

                // Shared by both card shapes below: this is what actually
                // fixes the tab-capture-mixes-everything bug (see
                // CHANNEL_HTML_TEMPLATE's doc comment) -- moving a channel's
                // real <audio> element out of this page and into its own tab.
                const separateTabToggle = `
                    <label class="checkbox-label" style="margin-bottom: 0.25rem;">
                        <input type="checkbox" id="ch-septab-${ch.id}" ${ch.separateTab ? 'checked' : ''}
                               onchange="toggleSeparateTab(${ch.id}, this.checked)">
                        <span>Open in a separate tab (share only that tab to keep channels apart)</span>
                    </label>
                `;

                if (ch.separateTab) {
                    // No local <audio>/device-select here on purpose -- both
                    // live in the other tab now (see openChannelTab). This
                    // card is a remote control + readout, driven entirely by
                    // simControl.onmessage's 'status'/'level' relays.
                    card.innerHTML = `
                        <div class="channel-header">
                            <div class="channel-tag ${ch.tagClass}">${ch.name}</div>
                            ${channels.length > 1 ? `<button class="btn btn-secondary" style="padding:0.2rem 0.5rem; font-size:0.75rem;" onclick="removeChannel(${ch.id})">✖ Remove</button>` : ''}
                        </div>

                        ${separateTabToggle}

                        <div class="track-info-box">
                            <div class="track-name" id="ch-title-${ch.id}">${ch.trackTitle}</div>
                            <div class="track-path-sub" id="ch-path-${ch.id}">${ch.trackPath}</div>
                        </div>

                        <div class="audio-wrapper">
                            <div class="channel-controls-row" style="justify-content: flex-start; gap: 0.5rem;">
                                <button class="btn btn-secondary" onclick="openChannelTab(channels.find(c => c.id === ${ch.id}))">🗔 Open/Focus Tab</button>
                                <button class="btn btn-secondary" onclick="sendPlay(${ch.id})">▶</button>
                                <button class="btn btn-secondary" onclick="sendPause(${ch.id})">⏸</button>
                                <button class="btn btn-secondary" onclick="sendRestart(${ch.id})">⏮</button>
                            </div>
                            <div class="level-meter-row">
                                <span>🔈 Level</span>
                                <div class="level-meter-track" id="ch-level-track-${ch.id}" role="meter"
                                     aria-label="${ch.name} output level" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
                                    <div class="level-meter-fill" id="ch-level-fill-${ch.id}"></div>
                                </div>
                            </div>
                            <div class="channel-controls-row">
                                <label class="checkbox-label">
                                    <input type="checkbox" id="ch-loop-${ch.id}" ${ch.loop ? 'checked' : ''}
                                           onchange="toggleChannelLoop(${ch.id}, this.checked)">
                                    <span>Loop Track</span>
                                </label>
                                <span id="ch-status-${ch.id}">Not opened yet</span>
                            </div>
                            <p style="font-size: 0.75rem; color: var(--text-muted);">
                                Output device is picked from inside that tab, not here.
                            </p>
                        </div>
                    `;
                    channelsContainer.appendChild(card);
                    return;
                }

                card.innerHTML = `
                    <div class="channel-header">
                        <div class="channel-tag ${ch.tagClass}">${ch.name}</div>
                        ${channels.length > 1 ? `<button class="btn btn-secondary" style="padding:0.2rem 0.5rem; font-size:0.75rem;" onclick="removeChannel(${ch.id})">✖ Remove</button>` : ''}
                    </div>

                    ${separateTabToggle}

                    <div class="device-selector-group">
                        <label for="ch-device-select-${ch.id}">🔊 macOS Output Sound Device:</label>
                        <select id="ch-device-select-${ch.id}" class="device-select" onchange="changeChannelDevice(${ch.id}, this.value)">
                            <option value="default">Default macOS Output</option>
                        </select>
                    </div>

                    <div class="track-info-box">
                        <div class="track-name" id="ch-title-${ch.id}">${ch.trackTitle}</div>
                        <div class="track-path-sub" id="ch-path-${ch.id}">${ch.trackPath}</div>
                    </div>

                    <div class="audio-wrapper">
                        <audio id="ch-audio-${ch.id}" controls preload="auto"></audio>
                        <div class="level-meter-row">
                            <span>🔈 Level</span>
                            <div class="level-meter-track" id="ch-level-track-${ch.id}" role="meter"
                                 aria-label="${ch.name} output level" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
                                <div class="level-meter-fill" id="ch-level-fill-${ch.id}"></div>
                            </div>
                        </div>
                        <div class="channel-controls-row">
                            <label class="checkbox-label">
                                <input type="checkbox" id="ch-loop-${ch.id}" ${ch.loop ? 'checked' : ''} onchange="toggleChannelLoop(${ch.id}, this.checked)">
                                <span>Loop Track</span>
                            </label>
                            <span id="ch-status-${ch.id}">Ready</span>
                        </div>
                    </div>
                `;

                channelsContainer.appendChild(card);

                // Surface load failures instead of leaving the status line stuck on "Ready"
                // forever — with no listener here, a stalled/reset stream or an undecodable
                // file (e.g. an unsupported codec) is silently invisible in the UI.
                const audioEl = card.querySelector(`#ch-audio-${ch.id}`);
                const statusEl = card.querySelector(`#ch-status-${ch.id}`);
                if (audioEl && statusEl) {
                    audioEl.addEventListener('error', () => {
                        const code = audioEl.error ? audioEl.error.code : 0;
                        const messages = {
                            1: 'Load aborted',
                            2: 'Network error (connection reset or server unreachable)',
                            3: 'Decode error (unsupported/corrupt audio)',
                            4: 'Format not supported by this browser'
                        };
                        statusEl.textContent = `⚠ ${messages[code] || 'Failed to load'}`;
                        statusEl.style.color = '#ef4444';
                    });
                    audioEl.addEventListener('stalled', () => {
                        statusEl.textContent = '⏳ Stalled (waiting on server)...';
                    });
                    audioEl.addEventListener('canplaythrough', () => {
                        statusEl.textContent = 'Ready';
                        statusEl.style.color = '';
                    });
                    // Listens for 'play' rather than being called from
                    // startChannelPlayback() so the meter attaches no matter
                    // how playback actually started -- Master Play, Restart,
                    // or the native <audio controls> widget's own button,
                    // which bypasses this page's JS entirely.
                    audioEl.addEventListener('play', () => startLevelMeter(ch, audioEl));
                }
            });

            updateAllDeviceDropdowns();
        }

        async function changeChannelDevice(chId, deviceId) {
            // Not reachable for a separate-tab channel -- its card renders
            // no device dropdown at all (see renderChannels), the tab
            // itself owns that choice. Guarded anyway rather than assumed.
            const ch = channels.find(c => c.id === chId);
            if (!ch || ch.separateTab) return;
            ch.deviceId = deviceId;

            const audioEl = document.getElementById(`ch-audio-${chId}`);
            if (audioEl && typeof audioEl.setSinkId === 'function') {
                try {
                    await audioEl.setSinkId(deviceId);
                    console.log(`Channel ${chId} output set to deviceId: ${deviceId}`);
                } catch (err) {
                    alert(`Failed to route audio to selected device: ${err.message}`);
                }
            } else if (audioEl) {
                console.warn("setSinkId is not supported in this browser or context.");
            }
        }

        function toggleChannelLoop(chId, isChecked) {
            const ch = channels.find(c => c.id === chId);
            if (ch) ch.loop = isChecked; // remembered either way, for a separate tab's next sendLoad
            if (ch && ch.separateTab) {
                simControl.postMessage({ kind: 'setLoop', channelId: chId, loop: isChecked });
                return;
            }
            const audioEl = document.getElementById(`ch-audio-${chId}`);
            if (audioEl) audioEl.loop = isChecked;
        }

        function assignFileToChannel(chId, filePath, fileName) {
            const ch = channels.find(c => c.id === chId);
            if (!ch) return;

            ch.trackTitle = fileName;
            ch.trackPath = filePath;

            const titleEl = document.getElementById(`ch-title-${chId}`);
            const pathEl = document.getElementById(`ch-path-${chId}`);

            if (titleEl) titleEl.textContent = fileName;
            if (pathEl) pathEl.textContent = filePath;

            if (ch.separateTab) {
                sendLoad(ch);
                return;
            }

            const audioEl = document.getElementById(`ch-audio-${chId}`);
            if (audioEl) {
                const streamUrl = getApiUrl('api/stream', { path: filePath });
                audioEl.src = streamUrl;
                if (ch.deviceId && typeof audioEl.setSinkId === 'function') {
                    audioEl.setSinkId(ch.deviceId).catch(console.error);
                }
            }
        }

        function removeChannel(chId) {
            channels = channels.filter(c => c.id !== chId);
            renderChannels();
            loadDirectory(currentDir);
        }

        btnAddChannel.onclick = () => {
            const nextId = channels.length > 0 ? Math.max(...channels.map(c => c.id)) + 1 : 1;
            const tagClasses = ["channel-tag-1", "channel-tag-2", "channel-tag-3"];
            const tagClass = tagClasses[(nextId - 1) % tagClasses.length];

            channels.push({
                id: nextId,
                name: `Channel ${nextId}`,
                tagClass: tagClass,
                trackTitle: "No Audio Assigned",
                trackPath: "Select audio file from table...",
                deviceId: "default",
                separateTab: false,
                loop: false
            });

            renderChannels();
            loadDirectory(currentDir);
        };

        // Master Controls

        // Firing .play() on every channel in the same tick used to be fire-and-forget: a
        // channel whose stream hadn't buffered enough (readyState < HAVE_FUTURE_DATA) just sat
        // there silently, and any rejection (AbortError from a racing currentTime write, a
        // NotAllowedError from autoplay policy, etc.) went only to console.error — invisible to
        // whoever is looking at the page. This waits for playability per channel (bounded, so a
        // truly stuck stream doesn't hang the button forever) and writes failures into that
        // channel's own status line.
        function waitUntilPlayable(audioEl, timeoutMs = 8000) {
            if (audioEl.readyState >= 3 /* HAVE_FUTURE_DATA */) return Promise.resolve();
            return new Promise((resolve, reject) => {
                const onReady = () => { cleanup(); resolve(); };
                const onError = () => { cleanup(); reject(audioEl.error || new Error('load error')); };
                const timer = setTimeout(() => { cleanup(); reject(new Error('timed out waiting to buffer')); }, timeoutMs);
                function cleanup() {
                    clearTimeout(timer);
                    audioEl.removeEventListener('canplay', onReady);
                    audioEl.removeEventListener('error', onError);
                }
                audioEl.addEventListener('canplay', onReady);
                audioEl.addEventListener('error', onError);
            });
        }

        async function startChannelPlayback(ch, { restart } = {}) {
            const audioEl = document.getElementById(`ch-audio-${ch.id}`);
            const statusEl = document.getElementById(`ch-status-${ch.id}`);
            if (!audioEl || !audioEl.src) return;

            try {
                await waitUntilPlayable(audioEl);
                if (restart) audioEl.currentTime = 0;
                await audioEl.play();
                if (statusEl) { statusEl.textContent = '▶ Playing'; statusEl.style.color = ''; }
            } catch (err) {
                console.error(`Channel ${ch.id} failed to start:`, err);
                if (statusEl) {
                    statusEl.textContent = `⚠ Failed to start: ${err.message || err}`;
                    statusEl.style.color = '#ef4444';
                }
            }
        }

        btnMasterPlay.onclick = () => {
            channels.forEach(ch => ch.separateTab ? sendPlay(ch.id) : startChannelPlayback(ch));
        };

        btnMasterPause.onclick = () => {
            channels.forEach(ch => {
                if (ch.separateTab) { sendPause(ch.id); return; }
                const audioEl = document.getElementById(`ch-audio-${ch.id}`);
                if (audioEl) audioEl.pause();
            });
        };

        btnMasterRestart.onclick = () => {
            channels.forEach(ch => ch.separateTab ? sendRestart(ch.id) : startChannelPlayback(ch, { restart: true }));
        };

        // Directory Navigation
        function setPath(newPath) {
            pathInput.value = newPath;
            loadDirectory(newPath);
        }

        function buildBreadcrumbs(pathStr) {
            breadcrumbsEl.innerHTML = "";
            const parts = pathStr.split('/').filter(p => p.length > 0);
            let accumulated = "";
            
            const rootSpan = document.createElement('span');
            rootSpan.className = 'crumb-item';
            rootSpan.textContent = '/';
            rootSpan.onclick = () => setPath('/');
            breadcrumbsEl.appendChild(rootSpan);

            parts.forEach((part, index) => {
                accumulated += '/' + part;
                const sep = document.createElement('span');
                sep.className = 'crumb-separator';
                sep.textContent = ' / ';
                breadcrumbsEl.appendChild(sep);

                const item = document.createElement('span');
                item.className = 'crumb-item';
                item.textContent = part;
                const targetPath = accumulated;
                item.onclick = () => setPath(targetPath);
                breadcrumbsEl.appendChild(item);
            });
        }

        async function loadDirectory(targetPath) {
            try {
                fileListBody.innerHTML = `<tr><td colspan="3" class="empty-state">Loading contents of ${targetPath}...</td></tr>`;
                const listUrl = getApiUrl('api/list', { path: targetPath });
                const res = await fetch(listUrl);
                const data = await res.json();

                if (data.error) {
                    fileListBody.innerHTML = `<tr><td colspan="3" class="empty-state" style="color: #ef4444;">Error: ${data.error}</td></tr>`;
                    return;
                }

                currentDir = data.current_dir;
                pathInput.value = currentDir;
                buildBreadcrumbs(currentDir);

                renderFileList(data.items);
            } catch (err) {
                fileListBody.innerHTML = `<tr><td colspan="3" class="empty-state" style="color: #ef4444;">Failed to connect to server: ${err.message}</td></tr>`;
            }
        }

        function renderFileList(items) {
            const filterText = filterInput.value.toLowerCase().trim();
            const filtered = items.filter(item => item.name.toLowerCase().includes(filterText));

            fileCountLabel.textContent = `${filtered.length} item(s)`;

            if (filtered.length === 0) {
                fileListBody.innerHTML = `<tr><td colspan="3" class="empty-state">No matching files or folders found.</td></tr>`;
                return;
            }

            fileListBody.innerHTML = "";

            filtered.forEach(item => {
                const tr = document.createElement('tr');
                
                const tdName = document.createElement('td');
                const nameDiv = document.createElement('div');
                nameDiv.className = 'item-name';
                
                const icon = document.createElement('span');
                icon.className = 'item-icon';
                if (item.is_dir) {
                    icon.textContent = '📁';
                } else if (item.is_audio) {
                    icon.textContent = '🎵';
                } else {
                    icon.textContent = '📄';
                }

                const nameText = document.createElement('span');
                nameText.textContent = item.name;

                nameDiv.appendChild(icon);
                nameDiv.appendChild(nameText);

                if (item.is_dir) {
                    nameDiv.onclick = () => setPath(item.path);
                }
                tdName.appendChild(nameDiv);

                const tdSize = document.createElement('td');
                tdSize.className = 'size-col';
                tdSize.textContent = item.is_dir ? '--' : item.size_formatted;

                const tdAction = document.createElement('td');
                tdAction.className = 'action-col';

                if (item.is_audio) {
                    channels.forEach(ch => {
                        const btn = document.createElement('button');
                        btn.className = `btn-assign btn-assign-ch${(ch.id % 2) + 1}`;
                        btn.textContent = `▶ Ch ${ch.id}`;
                        btn.onclick = () => assignFileToChannel(ch.id, item.path, item.name);
                        tdAction.appendChild(btn);
                    });
                } else if (item.is_dir) {
                    const btnOpen = document.createElement('button');
                    btnOpen.className = 'btn-secondary';
                    btnOpen.style.padding = '0.25rem 0.6rem';
                    btnOpen.style.fontSize = '0.8rem';
                    btnOpen.style.borderRadius = '4px';
                    btnOpen.textContent = 'Open';
                    btnOpen.onclick = () => setPath(item.path);
                    tdAction.appendChild(btnOpen);
                }

                tr.appendChild(tdName);
                tr.appendChild(tdSize);
                tr.appendChild(tdAction);

                fileListBody.appendChild(tr);
            });
        }

        btnGo.onclick = () => {
            if (pathInput.value.trim()) setPath(pathInput.value.trim());
        };

        pathInput.onkeydown = (e) => {
            if (e.key === 'Enter') btnGo.click();
        };

        btnUp.onclick = () => {
            if (currentDir) {
                const parent = currentDir.substring(0, currentDir.lastIndexOf('/')) || '/';
                setPath(parent);
            }
        };

        filterInput.oninput = () => {
            loadDirectory(currentDir);
        };

        // Initialize
        renderChannels();
        enumerateDevices();
        setPath(INITIAL_PATH);
    </script>
</body>
</html>
"""

# Single-channel player, opened in its own real browser tab by the master
# page's "Open in a separate tab" checkbox (see toggleSeparateTab in
# HTML_TEMPLATE).
#
# Why this exists: getDisplayMedia's tab-audio capture taps a tab's *whole*
# combined render output as one signal -- confirmed against the Screen
# Capture spec's suppressLocalAudioPlayback text ("that audio MUST still be
# captured by any ongoing audio-capturing capture-sessions"), which only
# makes sense if capture happens upstream of/parallel to setSinkId's
# per-element output routing. There is no per-sink scoping anywhere in the
# capture pipeline. So two <audio> elements in the same tab routed to two
# different macOS devices -- exactly what the master page's per-channel
# device dropdown does -- still both land in a single tab-share capture,
# defeating the entire point of the simulator: sharing "Channel 1's tab"
# for the my-meeting-notes app's browser-tab source would still pick up
# whatever Channel 2 is playing in that same tab, regardless of which
# physical device Channel 2 was routed to. Giving each channel its own tab
# fixes this the only way the browser actually allows: there is nothing
# else in that tab to bleed in.
#
# No server-side templating here (contrast with HTML_TEMPLATE's
# __INITIAL_PATH__ substitution): channel id/name come from this page's own
# query string via URLSearchParams, read client-side, so this template is a
# static string and there's no per-request string-formatting/escaping to
# get wrong.
CHANNEL_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title id="page-title">🎙️ Meeting Sim Channel</title>
    <style>
        :root {
            --bg-dark: #0f172a;
            --panel-bg: #1e293b;
            --panel-border: #334155;
            --accent-blue: #38bdf8;
            --accent-indigo: #6366f1;
            --accent-green: #10b981;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--bg-dark);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 2rem 1rem;
        }
        .card {
            background: linear-gradient(145deg, #1e293b 0%, #0f172a 100%);
            border: 1px solid var(--panel-border);
            border-radius: 16px;
            padding: 1.5rem;
            width: 100%;
            max-width: 480px;
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }
        h1 { font-size: 1.1rem; color: var(--accent-blue); word-break: break-word; }
        .hint { font-size: 0.8rem; color: var(--text-muted); line-height: 1.5; }
        select, audio { width: 100%; }
        select {
            background: #0f172a;
            border: 1px solid var(--panel-border);
            color: var(--text-main);
            padding: 0.6rem 1rem;
            border-radius: 8px;
            font-size: 0.9rem;
        }
        label { font-size: 0.75rem; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }
        .track-path { font-size: 0.75rem; color: var(--text-muted); word-break: break-all; }
        .level-meter-row { display: flex; align-items: center; gap: 0.5rem; font-size: 0.75rem; color: var(--text-muted); }
        .level-meter-track { flex: 1; height: 10px; background: rgba(15, 23, 42, 0.7); border: 1px solid var(--panel-border); border-radius: 999px; overflow: hidden; }
        .level-meter-fill { height: 100%; width: 0%; background: var(--accent-green); border-radius: 999px; transition: width 80ms linear, background-color 80ms linear; }
        #status { font-size: 0.85rem; }
    </style>
</head>
<body>
    <div class="card">
        <div>
            <h1 id="channel-title">Meeting Sim Channel</h1>
            <div class="track-path" id="track-path">Waiting for the master tab to assign a track...</div>
        </div>

        <div>
            <label for="device-select">🔊 macOS Output Sound Device (this tab only)</label>
            <select id="device-select" style="margin-top: 0.4rem;">
                <option value="default">Default macOS Output</option>
            </select>
        </div>

        <audio id="audio" controls preload="auto"></audio>

        <div class="level-meter-row">
            <span>🔈 Level</span>
            <div class="level-meter-track" id="level-track" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
                <div class="level-meter-fill" id="level-fill"></div>
            </div>
        </div>

        <div id="status">Waiting for the master tab...</div>

        <p class="hint">
            This tab plays exactly one channel, on purpose: sharing <em>this</em> tab's audio in the
            recording app captures only what's playing here, regardless of which output device is
            selected above. It's driven from the master tab's channel card (play/pause/restart, which
            file, loop) -- closing and reopening this tab via the master's "Open/Focus Tab" button
            resyncs it automatically. The native controls below still work locally if you need them.
        </p>
    </div>

    <script>
        const params = new URLSearchParams(location.search);
        const channelId = Number(params.get('channel'));
        const channelName = params.get('name') || `Channel ${channelId}`;
        document.title = `🎙️ ${channelName} — Meeting Sim`;
        document.getElementById('page-title').textContent = channelName;
        document.getElementById('channel-title').textContent = channelName;

        const audioEl = document.getElementById('audio');
        const statusEl = document.getElementById('status');
        const trackPathEl = document.getElementById('track-path');
        const deviceSelect = document.getElementById('device-select');

        // Same relative-path logic as the master page's getApiUrl, so this
        // still works behind a reverse proxy that mounts the whole app under
        // a path prefix (e.g. /simulator/) -- this page's own pathname is
        // that same prefix, since it's the master's index route with a
        // ?channel= query string, not a distinct path.
        function getApiUrl(endpoint, extraParams = {}) {
            let basePath = window.location.pathname;
            if (!basePath.endsWith('/')) basePath += '/';
            const url = new URL(basePath + endpoint, window.location.origin);
            Object.keys(extraParams).forEach((key) => url.searchParams.append(key, extraParams[key]));
            return url.toString();
        }

        const bc = new BroadcastChannel('meeting-sim-control');

        function setStatus(text, color) {
            statusEl.textContent = text;
            statusEl.style.color = color || '';
            bc.postMessage({ kind: 'status', channelId, text, color: color || '' });
        }

        async function enumerateDevices() {
            if (!navigator.mediaDevices || typeof navigator.mediaDevices.enumerateDevices !== 'function') return;
            try {
                const devices = await navigator.mediaDevices.enumerateDevices();
                const outputs = devices.filter((d) => d.kind === 'audiooutput');
                const currentVal = deviceSelect.value;
                deviceSelect.innerHTML = '<option value="default">Default macOS Output</option>';
                outputs.forEach((d) => {
                    if (d.deviceId === 'default') return;
                    const opt = document.createElement('option');
                    opt.value = d.deviceId;
                    opt.textContent = d.label || `Device (${d.deviceId.substring(0, 8)}...)`;
                    deviceSelect.appendChild(opt);
                });
                deviceSelect.value = currentVal || 'default';
            } catch (err) {
                console.error('Failed to enumerate devices:', err);
            }
        }

        deviceSelect.onchange = async () => {
            if (typeof audioEl.setSinkId !== 'function') {
                alert('setSinkId is not supported in this browser/context (needs HTTPS or localhost).');
                return;
            }
            try {
                await audioEl.setSinkId(deviceSelect.value);
                // Bookkeeping only -- the master never calls setSinkId for a
                // separate-tab channel itself, it just remembers this so a
                // closed-and-reopened tab (or a fresh 'load' resend) can be
                // told which device to reselect.
                bc.postMessage({ kind: 'deviceChanged', channelId, deviceId: deviceSelect.value });
            } catch (err) {
                alert(`Failed to route audio to selected device: ${err.message}`);
            }
        };

        function applyLoad(msg) {
            const streamUrl = getApiUrl('api/stream', { path: msg.path });
            // Comparing the full resolved URL, not msg.path, since that's
            // what audioEl.src actually holds -- a same-path 'hello' resend
            // would otherwise restart playback from a dead stop every time.
            if (audioEl.src !== streamUrl) {
                audioEl.src = streamUrl;
            }
            audioEl.loop = !!msg.loop;
            trackPathEl.textContent = msg.name ? `${msg.name} — ${msg.path}` : msg.path;
            if (msg.deviceId) {
                deviceSelect.value = msg.deviceId;
                if (typeof audioEl.setSinkId === 'function') {
                    audioEl.setSinkId(msg.deviceId).catch((err) => console.error('setSinkId failed:', err));
                }
            }
            setStatus('Loaded — ready');
        }

        async function startPlayback({ restart } = {}) {
            if (!audioEl.src) { setStatus('No track assigned yet', '#ef4444'); return; }
            try {
                if (restart) audioEl.currentTime = 0;
                await audioEl.play();
                setStatus('▶ Playing');
            } catch (err) {
                setStatus(`⚠ Failed to start: ${err.message || err}`, '#ef4444');
            }
        }

        bc.onmessage = (ev) => {
            const msg = ev.data;
            if (!msg || msg.channelId !== channelId) return;
            switch (msg.kind) {
                case 'load': applyLoad(msg); break;
                case 'play': startPlayback(); break;
                case 'pause': audioEl.pause(); break;
                case 'restart': startPlayback({ restart: true }); break;
                case 'setLoop': audioEl.loop = !!msg.loop; break;
            }
        };

        audioEl.addEventListener('pause', () => setStatus('⏸ Paused'));
        audioEl.addEventListener('error', () => {
            const code = audioEl.error ? audioEl.error.code : 0;
            const messages = {
                1: 'Load aborted', 2: 'Network error', 3: 'Decode error (unsupported/corrupt audio)',
                4: 'Format not supported by this browser',
            };
            setStatus(`⚠ ${messages[code] || 'Failed to load'}`, '#ef4444');
        });

        // Same tap-a-copy-not-the-real-signal-path level meter as the master
        // page's startLevelMeter -- see that function's doc comment for why
        // it's captureStream() into a disconnected AnalyserNode rather than
        // a MediaElementAudioSourceNode straight off audioEl (which would
        // hijack playback through this AudioContext's own destination
        // device, undoing setSinkId).
        let meter = null;
        function startLevelMeter() {
            if (audioEl.dataset.meterAttached === 'true') return;
            if (typeof audioEl.captureStream !== 'function') return;
            let stream;
            try {
                stream = audioEl.captureStream();
            } catch (err) {
                return;
            }
            if (stream.getAudioTracks().length === 0) return;

            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            void ctx.resume();
            const source = ctx.createMediaStreamSource(stream);
            const analyser = ctx.createAnalyser();
            analyser.fftSize = 1024;
            source.connect(analyser);
            audioEl.dataset.meterAttached = 'true';
            meter = { ctx, source, analyser };

            const fillEl = document.getElementById('level-fill');
            const trackEl = document.getElementById('level-track');
            const samples = new Uint8Array(analyser.fftSize);
            let level = 0;
            let lastPosted = -1;

            function tick() {
                if (!meter) return;
                analyser.getByteTimeDomainData(samples);
                let peak = 0;
                for (let i = 0; i < samples.length; i++) {
                    const dev = Math.abs(samples[i] - 128);
                    if (dev > peak) peak = dev;
                }
                const now = Math.min(1, peak / 128);
                level = now > level ? now : level * 0.85;
                const pct = Math.round(level * 100);

                fillEl.style.width = `${pct}%`;
                fillEl.style.backgroundColor = pct > 85 ? '#ef4444' : pct > 60 ? '#f59e0b' : 'var(--accent-green)';
                trackEl.setAttribute('aria-valuenow', String(pct));
                // Only relay to the master when the rounded value actually
                // moves -- during silence that's a few messages a second
                // instead of 60, and BroadcastChannel has no coalescing of
                // its own.
                if (pct !== lastPosted) {
                    lastPosted = pct;
                    bc.postMessage({ kind: 'level', channelId, pct });
                }
                requestAnimationFrame(tick);
            }
            tick();

            stream.getAudioTracks()[0].addEventListener('ended', () => { meter = null; });
        }
        audioEl.addEventListener('play', startLevelMeter);

        // Announce we're alive so the master resends whatever this channel
        // should be showing/playing -- BroadcastChannel has no "replay
        // missed messages" concept, so a tab opened (or reopened) after the
        // master already sent 'load'/'setLoop' would otherwise sit blank
        // until the user re-picks a file from the master's file table.
        bc.postMessage({ kind: 'hello', channelId });
        enumerateDevices();
    </script>
</body>
</html>
"""

class ThreadedAudioServer(socketserver.ThreadingMixIn, HTTPServer):
    """Handles each connection on its own thread.

    Plain HTTPServer processes one connection at a time, and each /api/stream
    request blocks its worker for the full duration of a chunked range read
    (see handle_api_stream). With N channel <audio> elements all opening GETs
    at once (Play All Channels / Sync Restart), a single-threaded server
    serializes them: whichever channel isn't first in the accept queue stalls
    indefinitely, and one channel's connection reset can starve every other
    channel that's still waiting to even be accepted.
    """
    daemon_threads = True
    allow_reuse_address = True


class AudioServerHandler(BaseHTTPRequestHandler):
    initial_path = DEFAULT_DIR

    def log_message(self, format, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {args[0]} {args[1]} {args[2]}\n")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("", "/", "/index.html") or path.endswith("/"):
            # A ?channel= query string on the same index route serves the
            # single-channel player instead of the master page -- see
            # CHANNEL_HTML_TEMPLATE's module-level doc comment for why this
            # needs to be its own tab rather than another element on the
            # master page.
            if "channel" in query:
                self.handle_channel_page()
            else:
                self.handle_index()
        elif path == "/api/list" or path.endswith("/api/list"):
            self.handle_api_list(query)
        elif path == "/api/stream" or path.endswith("/api/stream"):
            self.handle_api_stream(query)
        else:
            self.send_error(404, f"Endpoint not found: {path}")

    def handle_channel_page(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        encoded = CHANNEL_HTML_TEMPLATE.encode('utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def handle_index(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        html_content = HTML_TEMPLATE.replace("__INITIAL_PATH__", self.initial_path.replace('\\', '\\\\').replace('"', '\\"'))
        encoded = html_content.encode('utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def handle_api_list(self, query):
        req_path = query.get('path', [self.initial_path])[0]
        req_path = os.path.abspath(req_path)

        if not os.path.exists(req_path):
            self.send_json({"error": f"Path does not exist on server: {req_path}"}, status=404)
            return

        if not os.path.isdir(req_path):
            req_path = os.path.dirname(req_path)

        items = []
        try:
            entries = os.scandir(req_path)
            for entry in sorted(entries, key=lambda e: (not e.is_dir(), e.name.lower())):
                is_dir = entry.is_dir()
                ext = Path(entry.name).suffix.lower()
                is_audio = ext in AUDIO_EXTENSIONS
                
                size = 0
                size_formatted = "--"
                if not is_dir:
                    try:
                        size = entry.stat().st_size
                        size_formatted = format_size(size)
                    except OSError:
                        pass

                items.append({
                    "name": entry.name,
                    "path": os.path.join(req_path, entry.name),
                    "is_dir": is_dir,
                    "is_audio": is_audio,
                    "size": size,
                    "size_formatted": size_formatted
                })

            self.send_json({
                "current_dir": req_path,
                "items": items
            })
        except PermissionError:
            self.send_json({"error": f"Permission denied accessing directory: {req_path}"}, status=403)
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def handle_api_stream(self, query):
        filepath = query.get('path', [''])[0]
        filepath = os.path.abspath(filepath)

        if not os.path.isfile(filepath):
            self.send_error(404, "Audio file not found")
            return

        file_size = os.path.getsize(filepath)
        mime_type, _ = mimetypes.guess_type(filepath)
        mime_type = mime_type or 'audio/mpeg'

        range_header = self.headers.get('Range')
        if range_header:
            # Parse the Range header on its own: a malformed header is a client bug worth a
            # real 400 response, not something to fold into the same handler as a mid-stream
            # socket drop (which has no response left to send).
            try:
                byte_range = range_header.strip().lower().replace('bytes=', '')
                parts = byte_range.split('-')
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if parts[1] else file_size - 1
            except (ValueError, IndexError):
                self.send_error(400, f"Malformed Range header: {range_header!r}")
                return

            if start >= file_size or end >= file_size:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{file_size}')
                self.end_headers()
                return

            length = end - start + 1

            self.send_response(206)
            self.send_header('Content-Type', mime_type)
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            self.send_header('Content-Length', str(length))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()

            # Headers are already on the wire at this point, so nothing below can turn into an
            # HTTP error response. A client-aborted request (seek, channel re-src, tab close)
            # shows up here as ConnectionResetError/BrokenPipeError — that's expected traffic,
            # not a bug, so it's logged quietly and NOT re-raised (BaseHTTPRequestHandler would
            # otherwise print its own noisy traceback for the exact same benign event).
            try:
                with open(filepath, 'rb') as f:
                    f.seek(start)
                    chunk_size = 64 * 1024
                    bytes_left = length
                    while bytes_left > 0:
                        read_len = min(chunk_size, bytes_left)
                        chunk = f.read(read_len)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        bytes_left -= len(chunk)
            except (ConnectionResetError, BrokenPipeError):
                sys.stderr.write(f"Range request for {filepath!r} aborted by client (connection reset)\n")
            except Exception as e:
                sys.stderr.write(f"Error handling range request: {e}\n")
        else:
            self.send_response(200)
            self.send_header('Content-Type', mime_type)
            self.send_header('Content-Length', str(file_size))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()

            try:
                with open(filepath, 'rb') as f:
                    chunk_size = 64 * 1024
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (ConnectionResetError, BrokenPipeError):
                sys.stderr.write(f"Full-file stream for {filepath!r} aborted by client (connection reset)\n")
            except Exception as e:
                sys.stderr.write(f"Error streaming file: {e}\n")

    def send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Multi-Channel Meeting Audio Simulator Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--dir", default=DEFAULT_DIR, help=f"Initial path to browse (default: {DEFAULT_DIR})")
    
    args = parser.parse_args()

    AudioServerHandler.initial_path = os.path.abspath(args.dir)

    server_address = (args.host, args.port)
    httpd = ThreadedAudioServer(server_address, AudioServerHandler)

    print("=" * 65)
    print(" 🎙️  MULTI-CHANNEL MEETING AUDIO SIMULATOR SERVER READY")
    print("=" * 65)
    print(f" ► Listening on     : http://{args.host}:{args.port}")
    print(f" ► Local Access     : http://localhost:{args.port}")
    print(f" ► Default Path     : {AudioServerHandler.initial_path}")
    print("=" * 65)
    print("Press Ctrl+C to stop the server.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server gracefully...")
        httpd.shutdown()

if __name__ == "__main__":
    main()
