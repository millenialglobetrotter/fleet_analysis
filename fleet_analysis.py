import json
import logging
import requests
from datetime import datetime, timedelta, timezone
import ast
import http.server
import socketserver
import threading
import random
import string
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

# Attempt to import MySQL connector
try:
    import mysql.connector
    from mysql.connector import Error
    MYSQL_AVAILABLE = True
except ImportError:
    MYSQL_AVAILABLE = False
    logging.warning("mysql-connector-python not found. Database features disabled.")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.getenv('FLEET_CONFIG_FILE', os.path.join(BASE_DIR, 'config.json'))


def _get_int_env(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logging.warning("Invalid integer for %s, using default %s", name, default)
        return default


def _get_env_or_default(name, default=None):
    value = os.getenv(name)
    if value is None or value == '':
        return default
    return value


HOST = _get_env_or_default('HOST', '0.0.0.0')
PORT = _get_int_env('PORT', 8000)
MAX_WORKERS = _get_int_env('FLEET_MAX_WORKERS', 40)

DATA_STORE = {"success": [], "failed": [], "total_eligible": 0}
SESSIONS = {}
SESSION_LOCK = threading.Lock()
DATA_LOCK = threading.Lock()
AUTH_TOKEN_CACHE = {}
TOKEN_EXPIRY_SECONDS = 25 * 60  # Refresh 5 min before the 30-min expiry

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def load_config():
    config = {}

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as file_handle:
                config = json.load(file_handle)
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Failed to load config file %s: %s", CONFIG_FILE, exc)

    sections = {
        'auth': {
            'base_url': 'FLEET_AUTH_BASE_URL',
            'endpoint': 'FLEET_AUTH_ENDPOINT',
            'client_id': 'FLEET_AUTH_CLIENT_ID',
            'client_secret': 'FLEET_AUTH_CLIENT_SECRET',
        },
        'vehicle_registry': {
            'base_url': 'FLEET_VEHICLE_REGISTRY_BASE_URL',
            'endpoint': 'FLEET_VEHICLE_REGISTRY_ENDPOINT',
        },
        'prediction_service': {
            'base_url': 'FLEET_PREDICTION_SERVICE_BASE_URL',
            'url_template': 'FLEET_PREDICTION_SERVICE_URL_TEMPLATE',
        },
        'decryption_service': {
            'base_url': 'FLEET_DECRYPTION_SERVICE_BASE_URL',
            'endpoint': 'FLEET_DECRYPTION_SERVICE_ENDPOINT',
            'client_id': 'FLEET_DECRYPTION_SERVICE_CLIENT_ID',
            'client_secret': 'FLEET_DECRYPTION_SERVICE_CLIENT_SECRET',
        },
        'database': {
            'host': 'FLEET_DB_HOST',
            'port': 'FLEET_DB_PORT',
            'user': 'FLEET_DB_USER',
            'password': 'FLEET_DB_PASSWORD',
            'database': 'FLEET_DB_NAME',
        },
    }

    merged = {}
    for section_name, env_map in sections.items():
        section_values = dict(config.get(section_name, {}))
        for key, env_name in env_map.items():
            section_values[key] = _get_env_or_default(env_name, section_values.get(key))
        merged[section_name] = section_values

    return merged

# ── HTML TEMPLATE ───────────────────────────────────────────────────────
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FLEET ANALYTICS</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600;700&display=swap');
        :root {
            --bg:#f3f4f6;--bg2:#ffffff;--bg3:#f9fafb;--bg4:#e5e7eb;
            --border:#e5e7eb;--text:#111827;--text2:#4b5563;--text3:#9ca3af;
            --c:#0891b2;--cg:#059669;--cr:#dc2626;--co:#d97706;--cp:#9333ea;--cy:#ca8a04;--cb:#2563eb;
        }
        *{margin:0;padding:0;box-sizing:border-box}
        html,body{height:100%;overflow:hidden;font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text)}
        
        /* Modal Styles */
        .modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;display:none;align-items:center;justify-content:center;backdrop-filter:blur(2px)}
        .modal-overlay.show{display:flex}
        .modal{background:var(--bg2);width:90vw;height:85vh;border-radius:12px;box-shadow:0 20px 25px -5px rgba(0,0,0,0.1),0 10px 10px -5px rgba(0,0,0,0.04);display:flex;flex-direction:column;overflow:hidden;border:1px solid var(--border)}
        .modal-header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;background:var(--bg3)}
        .modal-title{font-size:16px;font-weight:700;color:var(--c);text-transform:uppercase;letter-spacing:1px}
        .modal-body{flex:1;overflow:hidden;display:flex;padding:0}
        .filter-col{flex:1;border-right:1px solid var(--border);display:flex;flex-direction:column;min-width:200px}
        .filter-col:last-child{border-right:none}
        .filter-header{padding:10px 12px;background:var(--bg3);border-bottom:1px solid var(--border);font-size:11px;font-weight:600;color:var(--text3);font-family:'JetBrains Mono',monospace;text-transform:uppercase;letter-spacing:0.5px;display:flex;justify-content:space-between;align-items:center}
        /* Search Bar in Filter */
        .filter-search-wrap{padding:6px 8px;border-bottom:1px solid var(--border);background:var(--bg2)}
        .filter-search{width:100%;padding:5px 8px;font-size:11px;font-family:'JetBrains Mono',monospace;border:1px solid var(--border);border-radius:4px;background:var(--bg3);color:var(--text);outline:none;transition:border .15s}
        .filter-search:focus{border-color:var(--c)}
        .filter-search::placeholder{color:var(--text3)}
        .filter-list{flex:1;overflow-y:auto;padding:4px}
        .filter-item{padding:6px 10px;font-size:12px;color:var(--text2);cursor:pointer;border-radius:4px;margin-bottom:2px;display:flex;align-items:center; gap: 8px; transition:background 0.1s}
        .filter-item:hover{background:var(--bg4)}
        .filter-item.selected{background:rgba(8,145,178,0.1);color:var(--c);font-weight:500}
        .filter-item .check{width:14px;height:14px;border:1px solid var(--border);border-radius:3px;display:flex;align-items:center;justify-content:center;font-size:10px;color:white;transition:all 0.1s;flex-shrink:0}
        .filter-item.selected .check{background:var(--c);border-color:var(--c);content:'✓'}
        .modal-footer{padding:16px 20px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:12px;background:var(--bg2)}
        
        /* Layout Styles */
        #loginPage{display:flex;align-items:center;justify-content:center;height:100vh;background:linear-gradient(135deg,#f3f4f6 0%,#e5e7eb 100%)}
        .login-card{background:var(--bg2);padding:40px;border-radius:12px;box-shadow:0 10px 25px rgba(0,0,0,0.1);width:350px;text-align:center;border:1px solid var(--border)}
        .login-logo{font-size:24px;font-weight:800;color:var(--c);letter-spacing:1px;margin-bottom:24px;text-transform:uppercase}
        .login-input{width:100%;padding:12px;margin-bottom:16px;border:1px solid var(--border);border-radius:6px;font-size:14px;outline:none;transition:border .2s}
        .login-input:focus{border-color:var(--c)}
        .btn-login{width:100%;background:var(--c);color:#fff;border:none;padding:12px;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .2s}
        .btn-login:hover{opacity:0.9}
        .error-msg{color:var(--cr);font-size:12px;margin-top:10px;min-height:18px}
        #appPage{display:none;height:100%}
        .hdr{height:auto;min-height:56px;background:var(--bg2);border-bottom:1px solid var(--border);padding:12px 24px;display:flex;flex-direction:column;gap:10px;position:relative;z-index:100;flex-shrink:0;box-shadow:0 1px 2px rgba(0,0,0,0.05)}
        .hdr-top{display:flex;align-items:center;justify-content:space-between}
        .logo-text{font-family:'Arial',sans-serif;font-size:20px;font-weight:800;letter-spacing:1px;color:var(--c);text-transform:uppercase}
        .hdr-right{display:flex;align-items:center;gap:12px}
        .hdr-pill{font-family:'JetBrains Mono',monospace;font-size:11px;padding:4px 12px;border-radius:20px;background:var(--bg3);border:1px solid var(--border);color:var(--text2)}
        .hdr-pill b{color:var(--c)}
        .control-panel{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;background:var(--bg3);padding:10px;border-radius:8px;border:1px solid var(--border);flex:1}
        .cp-group{display:flex;flex-direction:column;gap:4px;flex:1}
        .cp-label{font-size:10px;font-family:'JetBrains Mono',monospace;color:var(--text3);text-transform:uppercase;font-weight:600}
        .cp-inputs{display:flex;gap:8px;align-items:center;width:100%}
        input[type="date"],input[type="time"]{font-family:'JetBrains Mono',monospace;font-size:12px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg2);color:var(--text);outline:none;flex:1}
        input:focus{border-color:var(--c)}
        .btn-primary{background:var(--c);color:#fff;border:none;padding:6px 16px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;transition:opacity .2s;font-family:'JetBrains Mono',monospace;white-space:nowrap;margin-left:auto}
        .btn-primary:hover{opacity:0.9}
        .btn-primary:disabled{opacity:0.5;cursor:not-allowed}
        .btn-secondary{background:var(--bg2);color:var(--text);border:1px solid var(--border);padding:6px 16px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;transition:all .2s;font-family:'JetBrains Mono',monospace;white-space:nowrap}
        .btn-secondary:hover{border-color:var(--c);color:var(--c)}
        .btn-logout{font-size:12px;color:var(--text3);text-decoration:underline;cursor:pointer;border:none;background:none}
        .layout{display:grid;grid-template-columns:280px 1fr;height:calc(100vh - 110px); overflow:hidden}
        .sidebar{background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
        .stitle{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--text3);padding:16px 16px 6px;font-weight:600}
        .hint{font-size:11px;color:var(--text3);padding:0 16px 10px;font-style:italic}
        .sidebar-content-split{flex:1;display:flex;flex-direction:column;overflow:hidden}
        .sidebar-half{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden}
        .file-list{flex:1;overflow-y:auto;padding:8px 10px}
        .file-list::-webkit-scrollbar{width:4px}
        .file-list::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:3px}
        .fbtn{width:100%;text-align:left;background:transparent;border:1px solid transparent;color:var(--text2);padding:8px 10px;border-radius:6px;font-size:11px;cursor:pointer;transition:all .1s;margin-bottom:2px;display:flex;align-items:center;gap:8px}
        .fbtn:hover{background:var(--bg3);color:var(--text)}
        .fbtn.sel{background:rgba(8,145,178,0.08);border-color:var(--c);color:var(--c);font-weight:600}
        .fbtn-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px}
        .fbtn-score{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text3);background:var(--bg);padding:1px 6px;border-radius:9px;flex-shrink:0}
        .fbtn.sel .fbtn-score{color:var(--c);background:rgba(8,145,178,0.1)}
        .fbtn-all{font-weight:700;border-bottom:1px solid var(--border);margin-bottom:6px;padding-bottom:8px}
        .clear-btn{width:100%;text-align:left;background:transparent;border:1px solid var(--border);color:var(--text3);padding:6px 10px;border-radius:6px;font-size:10px;font-family:'JetBrains Mono',monospace;cursor:pointer;transition:all .14s;margin-top:4px}
        .clear-btn:hover{border-color:var(--cr);color:var(--cr);background:rgba(220,38,38,0.05)}
        
        /* MAIN PANEL LAYOUT FIXES */
        .main{padding:20px;overflow-y:auto;display:flex;flex-direction:column;gap:16px; height: 100%}
        .main::-webkit-scrollbar{width:6px}
        .main::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:4px}
        
        .kpi-row{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
        .kcard{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:16px;position:relative;overflow:hidden;transition:border-color .2s;box-shadow:0 1px 2px rgba(0,0,0,0.03)}
        .kcard::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--accent,var(--c))}
        .kcard:hover{border-color:var(--accent,var(--c))}
        .kcard-label{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:0.5px;text-transform:uppercase;color:var(--text3);margin-bottom:6px}
        .kcard-val{font-size:22px;font-weight:700;line-height:1;color:var(--text)}
        .kcard-sub{font-size:11px;color:var(--text2);margin-top:4px}
        .score-row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
        .scard{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:16px;display:flex;gap:16px;align-items:center}
        .scard-info{flex:1;min-width:0}
        .scard-title{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px;font-family:'JetBrains Mono',monospace;margin-bottom:4px}
        .scard-val{font-size:24px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--text)}
        .scard-file{font-size:11px;color:var(--text2);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
        .grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
        .icard{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:16px;box-shadow:0 1px 2px rgba(0,0,0,0.03);display:flex;flex-direction:column}
        .ict{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:0.5px;text-transform:uppercase;color:var(--text3);margin-bottom:12px;display:flex;align-items:center;gap:6px}
        .ict .dot{width:6px;height:6px;border-radius:50%}
        .rlist{display:flex;flex-direction:column;gap:6px;overflow-y:auto;padding-right:4px}
        .rlist::-webkit-scrollbar{width:3px}
        .rlist::-webkit-scrollbar-thumb{background:var(--bg4)}
        .ritem{display:flex;justify-content:space-between;align-items:center;padding:4px 6px;background:var(--bg3);border-radius:4px;border-left:2px solid transparent; font-size: 13px;}
        .ritem span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text2); max-width:65%; text-align:left;}
        .ritem span:last-child{font-family:'JetBrains Mono',monospace;font-weight:600; text-align:right;}
        .ritem.clickable{cursor:pointer;transition:background 0.2s}
        .ritem.clickable:hover{background:var(--border)}
        
        /* Interactive Row Styling */
        .ir{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid var(--bg4); cursor: pointer; user-select: none; transition: background 0.2s;}
        .ir:last-child{margin-bottom:0;padding-bottom:0;border-bottom:none}
        .ir:hover{background:var(--bg3)}
        .ir-l{font-size:12px;color:var(--text2); display:flex; align-items: center; gap: 6px;}
        .ir-icon{font-size:10px; transition: transform 0.2s; opacity: 0.5;}
        .ir.expanded .ir-icon{transform: rotate(180deg); opacity: 1;}
        .ir-v{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;color:var(--text)}
        .ir-v.g{color:var(--cg)}.ir-v.w{color:var(--co)}.ir-v.b{color:var(--cr)}
        
        /* Drill Down Details */
        .drill-down{display:none; padding: 8px 12px; background: var(--bg3); border-radius: 6px; margin-top: -8px; margin-bottom: 12px; border: 1px solid var(--border); animation: fadeIn 0.3s ease;}
        @keyframes fadeIn{from{opacity:0;transform:translateY(-5px)}to{opacity:1;transform:translateY(0)}}
        .drill-down-row{display:flex; justify-content:space-between; font-size:11px; padding:4px 0; border-bottom:1px solid var(--bg4); flex-direction: column; gap: 2px;}
        .drill-down-row:last-child{border:none}
        .drill-down-row span:first-child{word-wrap:break-word;word-break:break-all; color: var(--text2); font-size: 10px;}
        .drill-down-v{font-family:'JetBrains Mono',monospace; font-weight:600; color:var(--c); align-self: flex-end;}
        
        /* NEW: Clickable drill down rows */
        .drill-down-row.clickable { cursor: pointer; border-radius: 4px; padding: 4px; border-bottom: 1px solid var(--border); margin-bottom: 2px; background: var(--bg2); }
        .drill-down-row.clickable:hover { background: var(--border); }
        .drill-down-row.clickable span:first-child { color: var(--c); text-decoration: underline; }

        .sec-lbl{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--text3);padding:4px 0;border-bottom:1px solid var(--border);margin-top:8px;margin-bottom:8px}
        .pbar-track{height:4px;background:var(--bg3);border-radius:2px;overflow:hidden;margin-top:4px}
        .pbar-fill{height:100%;border-radius:2px;transition:width .5s ease}
        .gear-row{display:flex;align-items:center;gap:8px;margin-bottom:6px}
        .gear-row:last-child{margin-bottom:0}
        .gear-lbl{width:24px;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text2)}
        .gear-track{flex:1;height:6px;background:var(--bg3);border-radius:3px;overflow:hidden}
        .gear-fill{height:100%;border-radius:3px;transition:width .5s}
        .gear-km{width:45px;text-align:right;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text)}
        .gear-pct{width:32px;text-align:right;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text3)}
        .waste-row{display:flex;align-items:center;gap:8px;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid var(--bg4)}
        .waste-row:last-child{margin-bottom:0;padding-bottom:0;border-bottom:none}
        .waste-label{font-size:12px;color:var(--text2);min-width:110px}
        .waste-bar-track{flex:1;height:6px;background:var(--bg3);border-radius:3px;overflow:hidden}
        .waste-bar-fill{height:100%;border-radius:3px;transition:width .5s}
        .waste-val{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;min-width:60px;text-align:right;color:var(--text)}
        .top-waster-card{background:var(--bg3);border-radius:6px;padding:10px;margin-bottom:8px;border-left:3px solid var(--cr); cursor: pointer; transition: background 0.2s; position: relative;}
        .top-waster-card:hover{background:var(--bg4)}
        .top-waster-id{font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;color:var(--cr);margin-bottom:6px;word-wrap:break-word;word-break:break-all;}
        .top-waster-detail{font-size:10px;color:var(--text2);display:flex;justify-content:space-between;padding:2px 0}
        .top-waster-detail span:last-child{font-family:'JetBrains Mono',monospace;font-weight:600;color:var(--text)}
        .waster-breakdown{display:none; margin-top:8px; padding-top:8px; border-top:1px dashed var(--border)}
        .waster-breakdown-row{display:flex; justify-content:space-between; font-size:10px; margin-bottom:2px}
        .btn-compare{width:100%;margin-top:8px;background:var(--c);color:#fff;border:none;padding:5px 10px;border-radius:5px;font-size:10px;font-family:'JetBrains Mono',monospace;font-weight:700;cursor:pointer;letter-spacing:0.5px;transition:opacity .2s}
        /* View toggle bar */
        .view-toggle-bar{display:none;align-items:center;gap:0;background:var(--bg2);border:1px solid var(--border);border-radius:8px;overflow:hidden;flex-shrink:0;box-shadow:0 1px 3px rgba(0,0,0,0.06)}
        .view-toggle-bar.show{display:flex}
        .vtab{flex:1;padding:7px 18px;font-size:11px;font-family:'JetBrains Mono',monospace;font-weight:600;letter-spacing:0.5px;border:none;background:transparent;color:var(--text3);cursor:pointer;transition:all .18s;text-transform:uppercase;white-space:nowrap}
        .vtab:hover{color:var(--text);background:var(--bg3)}
        .vtab.active{background:var(--c);color:#fff}
        .vtab:not(:last-child){border-right:1px solid var(--border)}
        #vtabClose{border-radius:0 8px 8px 0}
        .vtab-label{display:flex;align-items:center;gap:6px;justify-content:center}
        .btn-compare:hover{opacity:0.85}
        .leaderboard-row{display:flex;align-items:center;justify-content:space-between;padding:4px 6px;background:var(--bg3);border-radius:4px;border-left:2px solid transparent;font-size:12px;margin-bottom:4px}
        .leaderboard-row span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text2);max-width:60%;text-align:left}
        .leaderboard-row .lboard-score{font-family:'JetBrains Mono',monospace;font-weight:600;text-align:right}
        /* NEW: Leaderboard Add Button */
        .cmp-add-btn {
            width: 18px; height: 18px;
            border-radius: 3px;
            border: 1px solid var(--border);
            background: transparent;
            color: var(--text3);
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
            line-height: 1;
            cursor: pointer;
            display: flex; align-items: center; justify-content: center;
            transition: all 0.2s;
            flex-shrink: 0;
            margin-left: auto;
            margin-right: 8px;
        }
        .cmp-add-btn:hover { border-color: var(--c); color: var(--c); }
        .cmp-add-btn.active {
            background: var(--c);
            border-color: var(--c);
            color: white;
        }
        .leaderboard-row .lboard-cmp{font-size:9px;font-family:'JetBrains Mono',monospace;padding:2px 6px;border-radius:3px;background:var(--c);color:#fff;cursor:pointer;flex-shrink:0;margin-left:6px;border:none;transition:opacity .2s}
        .leaderboard-row .lboard-cmp:hover{opacity:0.85}
        .err-section{background:rgba(217,119,6,.05);border:1px solid rgba(217,119,6,.2);border-radius:8px;padding:16px}
        .err-title{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:1px;text-transform:uppercase;color:var(--co);margin-bottom:10px}
        .err-list{display:flex;flex-wrap:wrap;gap:6px}
        .err-tag{font-family:'JetBrains Mono',monospace;font-size:10px;padding:3px 8px;border-radius:4px;background:var(--bg2);border:1px solid rgba(217,119,6,.2);color:var(--co)}
        .empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:400px;color:var(--text3);gap:16px;border:1px dashed var(--border);border-radius:12px;background:var(--bg2)}
        .loader{position:fixed;inset:0;background:rgba(255,255,255,.85);z-index:9999;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;opacity:0;pointer-events:none;transition:opacity .25s}
        .loader.show{opacity:1;pointer-events:auto}
        .spin{width:28px;height:28px;border:3px solid var(--border);border-top-color:var(--c);border-radius:50%;animation:sp .7s linear infinite}
        @keyframes sp{to{transform:rotate(360deg)}}
        .toast{position:fixed;top:70px;right:24px;background:var(--bg2);border:1px solid var(--cg);border-radius:8px;padding:10px 20px;font-size:11px;color:var(--cg);z-index:9998;transform:translateX(120%);transition:transform .3s;font-family:'JetBrains Mono',monospace;box-shadow:0 4px 12px rgba(0,0,0,0.1);max-width:400px;word-wrap:break-word}
        .toast.err{border-color:var(--cr);color:var(--cr)}
        .toast.show{transform:translateX(0)}
        
        /* Comparison layout fixes */
        #cmpContent{height:100%;display:none;flex-direction:column;overflow:hidden;}
        .cmp-wrap{display:grid;grid-template-columns:1fr 1fr;height:100%;overflow:hidden}
        .cmp-col{overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px}
        .cmp-col::-webkit-scrollbar{width:4px}
        .cmp-col::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:3px}
        .cmp-col:first-child{border-right:1px solid var(--border)}
        .cmp-header{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;color:var(--c);padding:8px 10px;background:rgba(8,145,178,0.06);border:1px solid rgba(8,145,178,0.2);border-radius:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis; text-align: left; display: flex; align-items: center; justify-content: flex-start;}
        .cmp-score-row{display:flex;gap:10px}
        .cmp-score-box{flex:1;background:var(--bg3);border-radius:6px;padding:10px;text-align:center}
        .cmp-score-lbl{font-size:9px;font-family:'JetBrains Mono',monospace;color:var(--text3);text-transform:uppercase;margin-bottom:4px}
        .cmp-score-val{font-size:26px;font-weight:700;font-family:'JetBrains Mono',monospace}
    </style>
</head>
<body>
    <div id="loginPage">
        <div class="login-card">
            <div class="login-logo">FLEET ANALYTICS</div>
            <input type="text" id="lUser" class="login-input" placeholder="Client ID" autocomplete="off">
            <input type="password" id="lPass" class="login-input" placeholder="Client Secret">
            <button class="btn-login" id="btnLogin" onclick="attemptLogin()">SIGN IN</button>
            <div class="error-msg" id="lError"></div>
        </div>
    </div>

    <div id="appPage">
        <div class="hdr">
            <div class="hdr-top">
                <div class="logo"><div class="logo-text">FLEET ANALYTICS</div></div>
                <div class="hdr-right">
                    <div class="hdr-pill"><b id="hTotalVehicles">0</b> Total Eligible</div>
                    <div class="hdr-pill"><b id="hFileCount">0</b> Vehicles</div>
                    <button class="btn-logout" onclick="logout()">Logout</button>
                </div>
            </div>
            <div class="control-panel">
                <div class="cp-group">
                    <div class="cp-label">From Date</div>
                    <div class="cp-inputs"><input type="date" id="startDate" value="2026-05-09"></div>
                </div>
                <div class="cp-group">
                    <div class="cp-label">To Date</div>
                    <div class="cp-inputs"><input type="date" id="endDate" value="2026-05-09"></div>
                </div>
                <div class="cp-group">
                    <div class="cp-label">Start Time</div>
                    <div class="cp-inputs"><input type="time" id="startTime" value="00:00"></div>
                </div>
                <div class="cp-group">
                    <div class="cp-label">End Time</div>
                    <div class="cp-inputs"><input type="time" id="endTime" value="23:59"></div>
                </div>
                <button class="btn-secondary" id="btnFilter" onclick="openFilterModal()">Filter Vehicles (0)</button>
                <button class="btn-primary" id="btnFetch" onclick="runAnalysis()">FETCH DATA</button>
            </div>
        </div>

        <div class="layout">
            <div class="sidebar">
                <div class="stitle">Vehicle Ids</div>
                <div class="hint">Ctrl+Click to select multiple</div>
                <div class="sidebar-content-split">
                    <div class="sidebar-half">
                        <div class="file-list" id="fileList"></div>
                    </div>
                    <div style="border-top:1px solid var(--border);flex-shrink:0"></div>
                    <div class="sidebar-half">
                        <div class="stitle" style="padding:10px 16px 6px;color:var(--cr);margin-top:0">No Data / Errors</div>
                        <div class="rlist" id="failedList"></div>
                    </div>
                </div>
                <div style="padding:10px">
                    <button class="clear-btn" id="clearBtn">&#x2715; Clear All Vehicles</button>
                </div>
            </div>

            <!-- main panel: holds EITHER aggregate OR comparison -->
            <div id="mainPanel" class="main">
                <!-- View toggle bar — visible when 2+ vehicles selected manually -->
                <div id="viewToggleBar" class="view-toggle-bar">
                    <button class="vtab active" id="vtabAgg" onclick="setViewMode('agg')">
                        <span class="vtab-label">&#9634; Aggregate</span>
                    </button>
                    <button class="vtab" id="vtabCmp" onclick="setViewMode('cmp')">
                        <span class="vtab-label">&#8942; Compare</span>
                    </button>
                    <button id="vtabClose" onclick="closeCompareView()" title="Back to previous view" style="flex:0;padding:7px 12px;font-size:13px;font-family:'JetBrains Mono',monospace;font-weight:700;border:none;border-left:1px solid var(--border);background:transparent;color:var(--text3);cursor:pointer;transition:all .15s;line-height:1" onmouseover="this.style.background='rgba(220,38,38,0.08)';this.style.color='var(--cr)'" onmouseout="this.style.background='transparent';this.style.color='var(--text3)'">&#x2715;</button>
                </div>
                <div id="emptyState" class="empty">
                    <div style="font-size:40px;opacity:.2">&#128194;</div>
                    <div>No data loaded</div>
                    <div style="font-size:12px">Select Dates, Filter Vehicles and Click 'Fetch Data' to begin analysis</div>
                </div>
                <div id="aggContent" style="display:none;flex-direction:column;gap:16px">
                    <!-- KPI row -->
                    <div class="kpi-row">
                        <div class="kcard" style="--accent:var(--c)">
                            <div class="kcard-label">Total Distance</div>
                            <div class="kcard-val" id="kTotalDist">&#8212;</div>
                            <div class="kcard-sub">all selected trips</div>
                        </div>
                        <div class="kcard" style="--accent:var(--co)">
                            <div class="kcard-label">Total Fuel</div>
                            <div class="kcard-val" id="kTotalFuel">&#8212;</div>
                            <div class="kcard-sub">consumed</div>
                        </div>
                        <div class="kcard" style="--accent:var(--cp)">
                            <div class="kcard-label">Avg Economy</div>
                            <div class="kcard-val" id="kAvgEcon">&#8212;</div>
                            <div class="kcard-sub">km per litre</div>
                        </div>
                    </div>
                    <!-- Score row -->
                    <div class="score-row">
                        <div class="scard">
                            <svg width="60" height="60" viewBox="0 0 64 64">
                                <circle cx="32" cy="32" r="25" fill="none" stroke="var(--border)" stroke-width="5"/>
                                <circle id="driverArc" cx="32" cy="32" r="25" fill="none" stroke="var(--cg)" stroke-width="5" stroke-dasharray="0 157" stroke-linecap="round" transform="rotate(-90 32 32)" style="transition:stroke-dasharray .6s ease"/>
                                <text x="32" y="36" text-anchor="middle" font-family="JetBrains Mono,monospace" font-size="11" fill="var(--cg)" id="driverArcLbl">&#8212;</text>
                            </svg>
                            <div class="scard-info">
                                <div class="scard-title">Avg Driver Score</div>
                                <div class="scard-val" id="topDriverScore" style="color:var(--cg)">&#8212;</div>
                                <div class="scard-file">Average of selected</div>
                            </div>
                        </div>
                        <div class="scard">
                            <svg width="60" height="60" viewBox="0 0 64 64">
                                <circle cx="32" cy="32" r="25" fill="none" stroke="var(--border)" stroke-width="5"/>
                                <circle id="fuelArc" cx="32" cy="32" r="25" fill="none" stroke="var(--cg)" stroke-width="5" stroke-dasharray="0 157" stroke-linecap="round" transform="rotate(-90 32 32)" style="transition:stroke-dasharray .6s ease"/>
                                <text x="32" y="36" text-anchor="middle" font-family="JetBrains Mono,monospace" font-size="11" fill="var(--cg)" id="fuelArcLbl">&#8212;</text>
                            </svg>
                            <div class="scard-info">
                                <div class="scard-title">Avg Fuel Score</div>
                                <div class="scard-val" id="topFuelScore" style="color:var(--cg)">&#8212;</div>
                                <div class="scard-file">Average of selected</div>
                            </div>
                        </div>
                    </div>
                    <!-- LEADERBOARDS -->
                    <div id="scoreLeaderboardSection">
                        <div class="sec-lbl">&#9656; SCORES LEADERBOARD</div>
                        <div class="grid2">
                            <div class="icard">
                                <div class="ict"><span class="dot" style="background:var(--cg)"></span>Top 3 Driver Scores</div>
                                <div class="rlist" id="topDriverList"></div>
                            </div>
                            <div class="icard">
                                <div class="ict"><span class="dot" style="background:var(--cr)"></span>Bottom 3 Driver Scores</div>
                                <div class="rlist" id="botDriverList"></div>
                            </div>
                        </div>
                        <div class="grid2">
                            <div class="icard">
                                <div class="ict"><span class="dot" style="background:var(--cg)"></span>Top 3 Fuel Scores</div>
                                <div class="rlist" id="topFuelList"></div>
                            </div>
                            <div class="icard">
                                <div class="ict"><span class="dot" style="background:var(--cr)"></span>Bottom 3 Fuel Scores</div>
                                <div class="rlist" id="botFuelList"></div>
                            </div>
                        </div>
                    </div>
                    <!-- FUEL ANALYSIS -->
                    <div class="sec-lbl">&#9656; FUEL ANALYSIS</div>
                    <div class="grid3">
                        <div class="icard">
                            <div class="ict"><span class="dot" style="background:var(--cg)"></span>Efficiency</div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Fuel per 100 km</span><span class="ir-v" id="iFuelPer100">&#8212;</span></div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Best Economy Trip</span><span class="ir-v g" id="iBestEcon">&#8212;</span></div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Worst Economy Trip</span><span class="ir-v b" id="iWorstEcon">&#8212;</span></div>
                        </div>
                        <div class="icard">
                            <div class="ict"><span class="dot" style="background:var(--cr)"></span>Fuel Waste Sources</div>
                            <div class="waste-row">
                                <span class="waste-label">Idle</span>
                                <div class="waste-bar-track"><div class="waste-bar-fill" id="wBarIdle" style="background:var(--cr);width:0%"></div></div>
                                <span class="waste-val" id="wValIdle">&#8212;</span>
                            </div>
                            <div class="waste-row">
                                <span class="waste-label">Overspeed</span>
                                <div class="waste-bar-track"><div class="waste-bar-fill" id="wBarOver" style="background:var(--co);width:0%"></div></div>
                                <span class="waste-val" id="wValOver">&#8212;</span>
                            </div>
                            <div class="waste-row">
                                <span class="waste-label">Overrev</span>
                                <div class="waste-bar-track"><div class="waste-bar-fill" id="wBarRev" style="background:var(--cy);width:0%"></div></div>
                                <span class="waste-val" id="wValRev">&#8212;</span>
                            </div>
                            <div class="waste-row" style="border-bottom:none; margin-bottom:0; background:transparent; padding-top:8px;">
                                <span class="waste-label" style="font-weight:700">Total Waste</span>
                                <span class="waste-val b" id="iTotalWaste">&#8212;</span>
                                <span class="waste-val" id="iWastePct" style="margin-left:auto; min-width:auto; padding-left:10px;">&#8212;</span>
                            </div>
                        </div>
                        <div class="icard">
                            <div class="ict"><span class="dot" style="background:var(--cr)"></span>Top 2 Fuel Wasters</div>
                            <div id="topWastersList"></div>
                        </div>
                    </div>
                    <div class="grid3" style="margin-top:0">
                        <div class="icard" style="grid-column:span 3">
                            <div class="ict"><span class="dot" style="background:var(--cb)"></span>Usage Ratios</div>
                            <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px">
                                <div class="ir" style="flex-direction:column;align-items:flex-start;border:none;gap:4px; cursor: default">
                                    <span class="ir-l">Idle / Engine ON</span><span class="ir-v" id="iIdleRatio">&#8212;</span>
                                </div>
                                <div class="ir" style="flex-direction:column;align-items:flex-start;border:none;gap:4px; cursor: default">
                                    <span class="ir-l">Wrong Gear / Dist</span><span class="ir-v" id="iWrongGearPct">&#8212;</span>
                                </div>
                                <div class="ir" style="flex-direction:column;align-items:flex-start;border:none;gap:4px; cursor: default">
                                    <span class="ir-l">Half Clutch / Dist</span><span class="ir-v" id="iHalfClutchPct">&#8212;</span>
                                </div>
                                <div class="ir" style="flex-direction:column;align-items:flex-start;border:none;gap:4px; cursor: default">
                                    <span class="ir-l">Overspeed / Dist</span><span class="ir-v" id="iOverPct">&#8212;</span>
                                </div>
                                <div class="ir" style="flex-direction:column;align-items:flex-start;border:none;gap:4px; cursor: default">
                                    <span class="ir-l">Coasting Distance</span><span class="ir-v g" id="iCoasting">&#8212;</span>
                                </div>
                            </div>
                        </div>
                    </div>
                    <!-- SAFETY EVENTS -->
                    <div class="sec-lbl">&#9656; SAFETY EVENTS (Click to drill down)</div>
                    <div class="grid2">
                        <div class="icard">
                            <div class="ict"><span class="dot" style="background:var(--cr)"></span>Harsh Events</div>
                            <div class="ir" id="rowHarshBrake" onclick="toggleDrillDown(this)"><span class="ir-l">Harsh Braking <span class="ir-icon">&#9660;</span></span><span class="ir-v" id="iHarshBrake">&#8212;</span></div>
                            <div id="dd_Harsh_Braking" class="drill-down"></div>
                            
                            <div class="ir" id="rowHarshAcc" onclick="toggleDrillDown(this)"><span class="ir-l">Harsh Acceleration <span class="ir-icon">&#9660;</span></span><span class="ir-v" id="iHarshAcc">&#8212;</span></div>
                            <div id="dd_Harsh_Acceleration" class="drill-down"></div>
                            
                            <div class="ir" id="rowHarshCorn" onclick="toggleDrillDown(this)"><span class="ir-l">Harsh Cornering <span class="ir-icon">&#9660;</span></span><span class="ir-v" id="iHarshCorn">&#8212;</span></div>
                            <div id="dd_Harsh_Cornering" class="drill-down"></div>
                            
                            <div class="ir" style="cursor:default"><span class="ir-l">Total Harsh</span><span class="ir-v b" id="iTotalHarsh">&#8212;</span></div>
                        </div>
                        <div class="icard">
                            <div class="ict"><span class="dot" style="background:var(--co)"></span>Braking Events</div>
                            <div class="ir" id="rowModBrake" onclick="toggleDrillDown(this)"><span class="ir-l">Moderate Braking <span class="ir-icon">&#9660;</span></span><span class="ir-v" id="iModBrake">&#8212;</span></div>
                            <div id="dd_Moderate_Braking" class="drill-down"></div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Harsh Events/100km</span><span class="ir-v" id="iEventsPerKm">&#8212;</span></div>
                        </div>
                    </div>
                    <!-- SPEED PROFILE -->
                    <div class="sec-lbl">&#9656; SPEED PROFILE</div>
                    <div class="grid3">
                        <div class="icard">
                            <div class="ict"><span class="dot" style="background:var(--co)"></span>Speed Stats</div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Maximum Speed</span><span class="ir-v" id="iMaxSpeed">&#8212;</span></div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Avg Speed</span><span class="ir-v" id="iAvgSpd">&#8212;</span></div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Overspeed Distance</span><span class="ir-v w" id="iOverSpd">&#8212;</span></div>
                        </div>
                        <div class="icard" style="grid-column:span 2">
                            <div class="ict"><span class="dot" style="background:var(--cr)"></span>High Speed Events (&gt; 80 km/h)</div>
                            <div style="font-size:11px;color:var(--text2);margin-bottom:8px">Trips recording maximum speeds above 80 km/h</div>
                            <div class="rlist" id="highSpeedList"></div>
                        </div>
                    </div>
                    <!-- ENGINE -->
                    <div class="sec-lbl">&#9656; ENGINE &amp; DRIVETRAIN</div>
                    <div class="grid3">
                        <div class="icard">
                            <div class="ict"><span class="dot" style="background:var(--cb)"></span>Engine Time</div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Engine ON</span><span class="ir-v g" id="iEngOn">&#8212;</span></div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Engine OFF</span><span class="ir-v" id="iEngOff">&#8212;</span></div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Idle Duration</span><span class="ir-v w" id="iIdleTime">&#8212;</span></div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Data Loss Duration</span><span class="ir-v w" id="iDataLoss">&#8212;</span></div>
                        </div>
                        <div class="icard">
                            <div class="ict"><span class="dot" style="background:var(--cp)"></span>Start / Stop</div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Engine Start Count</span><span class="ir-v" id="iStartCnt">&#8212;</span></div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Engine Stop Count</span><span class="ir-v" id="iStopCnt">&#8212;</span></div>
                            <div class="ir" style="cursor:default"><span class="ir-l">MIL Error Distance</span><span class="ir-v w" id="iMilError">&#8212;</span></div>
                        </div>
                        <div class="icard">
                            <div class="ict"><span class="dot" style="background:var(--cy)"></span>Clutch &amp; Gear Wear</div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Half Clutch Distance</span><span class="ir-v w" id="iHalfClutch">&#8212;</span></div>
                            <div class="ir" style="cursor:default"><span class="ir-l">Wrong Gear Distance</span><span class="ir-v w" id="iWrongGear">&#8212;</span></div>
                        </div>
                    </div>
                    <!-- Gear -->
                    <div class="sec-lbl">&#9656; GEAR DISTRIBUTION</div>
                    <div class="icard">
                        <div class="ict"><span class="dot" style="background:var(--c)"></span>Distance per Gear</div>
                        <div id="gearBars"></div>
                    </div>
                    <div class="err-section" id="errSection" style="display:none">
                        <div class="err-title">&#9888; DATA QUALITY / ERROR SIGNALS</div>
                        <div class="err-list" id="errList"></div>
                    </div>
                </div>
                <!-- Comparison view — injected dynamically -->
                <div id="cmpContent" style="display:none"></div>
            </div>
        </div>
    </div>

    <!-- Vehicle Filter Modal -->
    <div class="modal-overlay" id="filterModal">
        <div class="modal">
            <div class="modal-header">
                <div class="modal-title">Select Vehicles</div>
                <div>
                    <button class="btn-secondary" style="margin-right:8px;font-size:10px;padding:4px 8px;" onclick="syncRegistry()">Sync Registry</button>
                    <button class="btn-secondary" style="font-size:10px;padding:4px 8px;" onclick="closeFilterModal()">&#x2715;</button>
                </div>
            </div>
            <div class="modal-body">
                <div class="filter-col">
                    <div class="filter-header">Make <span class="count" id="countMake">0</span></div>
                    <div class="filter-search-wrap"><input type="text" class="filter-search" id="searchMake" placeholder="Search make..." oninput="renderFilterLists()"></div>
                    <div class="filter-list" id="listMake"></div>
                </div>
                <div class="filter-col">
                    <div class="filter-header">Model <span class="count" id="countModel">0</span></div>
                    <div class="filter-search-wrap"><input type="text" class="filter-search" id="searchModel" placeholder="Search model..." oninput="renderFilterLists()"></div>
                    <div class="filter-list" id="listModel"></div>
                </div>
                <div class="filter-col">
                    <div class="filter-header">Variant <span class="count" id="countVariant">0</span></div>
                    <div class="filter-search-wrap"><input type="text" class="filter-search" id="searchVariant" placeholder="Search variant..." oninput="renderFilterLists()"></div>
                    <div class="filter-list" id="listVariant"></div>
                </div>
                <div class="filter-col">
                    <div class="filter-header">Vehicle ID <span class="count" id="countVehicle">0</span></div>
                    <div class="filter-search-wrap" style="display:flex;gap:6px;align-items:center">
                        <input type="text" class="filter-search" id="searchVehicle" placeholder="Search vehicle ID..." oninput="renderFilterLists()" style="flex:1">
                        <button id="btnSelectAllVehicles" onclick="toggleSelectAllVehicles()" style="font-size:9px;font-family:'JetBrains Mono',monospace;padding:3px 7px;border:1px solid var(--border);border-radius:4px;background:var(--bg3);color:var(--text2);cursor:pointer;white-space:nowrap;flex-shrink:0" title="Select / Deselect all visible vehicles">All</button>
                    </div>
                    <div class="filter-list" id="listVehicle"></div>
                </div>
            </div>
            <div class="modal-footer">
                <div style="font-size:11px;color:var(--text2);margin-right:auto" id="selectionSummary">0 selected</div>
                <button class="btn-secondary" onclick="clearFilters()">Clear</button>
                <button class="btn-primary" onclick="applyFilters()">Apply Selection</button>
            </div>
        </div>
    </div>

    <div class="loader" id="loader"><div class="spin"></div><div style="font-size:11px;color:var(--text2);font-family:'JetBrains Mono',monospace">Processing...</div></div>
    <div class="toast" id="toast"></div>

<script>
let days=[], selected={}, selectAll=true, nextId=0, viewMode='agg';
let prevSelection=null; // snapshot before a compare button overrides selection
let allVehicles = []; 
let selectedVehicleIds = new Set();
let filterState = {
    makes: new Set(),
    models: new Set(),
    variants: new Set()
};
// NEW: Set to track IDs manually selected via + button in leaderboards
let leaderboardSelection = new Set();

async function checkSession(){
    try{
        const r=await fetch('/api/check-session');
        const d=await r.json();
        document.getElementById('loginPage').style.display=d.logged_in?'none':'flex';
        document.getElementById('appPage').style.display=d.logged_in?'block':'none';
        if(d.logged_in) loadVehicleFilters(); 
    }catch(e){console.error(e)}
}

async function attemptLogin(){
    const clientId=document.getElementById('lUser').value.trim();
    const clientSecret=document.getElementById('lPass').value.trim();
    if(!clientId||!clientSecret){document.getElementById('lError').textContent='Enter Client ID and Secret';return}
    const btn=document.getElementById('btnLogin');
    btn.disabled=true; btn.textContent='Authenticating...';
    document.getElementById('lError').textContent='';
    try{
        const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:clientId,client_secret:clientSecret})});
        const d=await r.json();
        if(d.success) window.location.reload();
        else document.getElementById('lError').textContent=d.message||'Authentication failed';
    }catch(e){document.getElementById('lError').textContent='Server error'}
    finally{btn.disabled=false; btn.textContent='SIGN IN';}
}

async function logout(){await fetch('/api/logout',{method:'POST'});window.location.reload()}

function parseJsonToDay(raw,filename){
    let data=null;
    try{ data=typeof raw==='string'?JSON.parse(raw):(typeof raw==='object'&&raw!==null?raw:null); }catch(e){return null}
    if(!data) return null;
    const ins=data.Score_And_Insights||{},met=data.Metrics_Data||{},errs=data.ErrorValues||{};
    if(Array.isArray(ins)&&ins[0]===false) return null;
    function gv(o,k,fb=0){if(!o||!o[k])return fb;const i=o[k];return(typeof i==='object'&&i.value!==undefined)?i.value:fb}
    function gt(o,k,fb='00:00:00'){if(!o||!o[k])return fb;const i=o[k];return(typeof i==='object'&&i.value!==undefined)?i.value:fb}
    const gearData=gv(met,'Gear_Detection',{});
    return{
        _id:nextId++,
        date_label:filename.replace('.json','').replace(/_/g,' '),
        source:filename,
        driver_score:parseFloat(ins.Driver_Score||0),
        fuel_score:parseFloat(ins.Fuel_Score||0),
        stats:{
            distance_km:parseFloat(gv(met,'Distance_Travelled')),
            avg_speed:parseFloat(gv(met,'Average_Speed')),
            max_speed:parseFloat(gv(met,'Maximum_Speed')),
            total_fuel_l:parseFloat(gv(met,'Total_Fuel_Consumed')),
            fuel_economy:parseFloat(gv(met,'Fuel_Economy')),
            harsh_acc:parseFloat(gv(met,'Harsh_Acceleration')),
            harsh_brake:parseFloat(gv(met,'Harsh_Braking')),
            harsh_corner:parseFloat(gv(met,'Harsh_Cornering')),
            mod_brake:parseFloat(gv(met,'Moderate_Braking')),
            wrong_gear_km:parseFloat(gv(met,'Distance_Travelled_in_Wrong_Gear')),
            overspeed_km:parseFloat(gv(met,'Overspeeding_Distance')),
            coasting_km:parseFloat(gv(met,'Coasting_Distance')),
            half_clutch_km:parseFloat(gv(met,'Distance_Travelled_With_Half_Clutch')),
            idle_fuel_l:parseFloat(gv(met,'Additional_Fuel_Consumed_During_Engine_Idling')),
            overspeed_fuel_l:parseFloat(gv(met,'Additional_Fuel_Consumed_During_Overspeed')),
            overrev_fuel_l:parseFloat(gv(met,'Additional_Fuel_Consumption_During_Engine_Overreving')),
            mil_error_km:parseFloat(gv(met,'MIL_Error')),
            idle_time:gt(met,'Engine_Idling_Duration'),
            overrev_time:gt(met,'Engine_Overreving_Duration'),
            engine_on:gt(met,'Engine_ON_Time'),
            engine_off:gt(met,'Engine_OFF_Time'),
            data_loss:gt(met,'Data_Loss_Duration'),
            start_count:parseFloat(gv(met,'Engine_Start_Count')),
            stop_count:parseFloat(gv(met,'Engine_Stop_Count')),
            gear_dist:(gearData&&typeof gearData==='object')?gearData:{},
            err_signals:Object.keys(errs)
        }
    };
}

async function loadVehicleFilters(){
    try{
        const r=await fetch('/api/vehicles/filters');
        if(!r.ok) throw new Error('Failed to load filters');
        allVehicles=await r.json();
        renderFilterLists();
    }catch(e){
        console.error(e);
        showToast('Error loading vehicle filters',true);
    }
}

async function syncRegistry(){
    try{
        showToast('Syncing vehicle registry...', false);
        const r=await fetch('/api/vehicles/sync', {method:'POST'});
        if(r.ok){
            await loadVehicleFilters();
            showToast('Sync Complete');
        } else {
            showToast('Sync Failed', true);
        }
    }catch(e){
        showToast('Sync Error', true);
    }
}

function getUnique(arr, key){ return [...new Set(arr.map(item=>item[key]))].sort(); }

function getSearchVal(id){ return (document.getElementById(id)||{}).value||''; }

function renderFilterLists(){
    const searchMake = getSearchVal('searchMake').toLowerCase();
    const searchModel = getSearchVal('searchModel').toLowerCase();
    const searchVariant = getSearchVal('searchVariant').toLowerCase();
    const searchVehicle = getSearchVal('searchVehicle').toLowerCase();

    const makeArr = Array.from(filterState.makes);
    const modelArr = Array.from(filterState.models);
    const variantArr = Array.from(filterState.variants);

    // All makes (filtered by search)
    const allMakes = getUnique(allVehicles, 'make').filter(m => !searchMake || m.toLowerCase().includes(searchMake));

    // Vehicles visible after make filter
    let visAfterMake = allVehicles;
    if(makeArr.length) visAfterMake = visAfterMake.filter(v => makeArr.includes(v.make));

    // Models (filtered by make selection + search)
    const allModels = getUnique(visAfterMake, 'model').filter(m => !searchModel || m.toLowerCase().includes(searchModel));

    // Vehicles visible after make + model filter
    let visAfterModel = visAfterMake;
    if(modelArr.length) visAfterModel = visAfterModel.filter(v => modelArr.includes(v.model));

    // Variants (filtered by make+model selection + search)
    const allVariants = getUnique(visAfterModel, 'variant').filter(v => !searchVariant || v.toLowerCase().includes(searchVariant));

    // Vehicles visible after make + model + variant filter
    let visibleVehicles = visAfterModel;
    if(variantArr.length) visibleVehicles = visibleVehicles.filter(v => variantArr.includes(v.variant));

    // Vehicle IDs (filtered by search)
    const vehicleList = visibleVehicles.filter(v => !searchVehicle || v.vehicle_id.toLowerCase().includes(searchVehicle));

    // Render Makes
    const mkHtml = allMakes.map(m => `<div class="filter-item ${filterState.makes.has(m)?'selected':''}" onclick="toggleFilter('make', '${m.replace(/'/g,"\\'")}')"><div class="check">${filterState.makes.has(m)?'&#x2713;':''}</div>${m}</div>`).join('');
    document.getElementById('listMake').innerHTML = mkHtml || '<div style="padding:10px;color:#999;font-size:11px">No results</div>';
    document.getElementById('countMake').textContent = allMakes.length;

    // Render Models
    const mdHtml = allModels.map(m => `<div class="filter-item ${filterState.models.has(m)?'selected':''}" onclick="toggleFilter('model', '${m.replace(/'/g,"\\'")}')"><div class="check">${filterState.models.has(m)?'&#x2713;':''}</div>${m}</div>`).join('');
    document.getElementById('listModel').innerHTML = mdHtml || '<div style="padding:10px;color:#999;font-size:11px">' + (makeArr.length?'No results':'Select a Make') + '</div>';
    document.getElementById('countModel').textContent = allModels.length;

    // Render Variants
    const vrHtml = allVariants.map(v => `<div class="filter-item ${filterState.variants.has(v)?'selected':''}" onclick="toggleFilter('variant', '${v.replace(/'/g,"\\'")}')"><div class="check">${filterState.variants.has(v)?'&#x2713;':''}</div>${v}</div>`).join('');
    document.getElementById('listVariant').innerHTML = vrHtml || '<div style="padding:10px;color:#999;font-size:11px">' + (modelArr.length?'No results':'Select a Model') + '</div>';
    document.getElementById('countVariant').textContent = allVariants.length;

    // Render Vehicle IDs
    const vhHtml = vehicleList.map(v => {
        const sel = selectedVehicleIds.has(v.vehicle_id);
        return `<div class="filter-item ${sel?'selected':''}" onclick="toggleVehicle('${v.vehicle_id.replace(/'/g,"\\'")}')"><div class="check">${sel?'&#x2713;':''}</div>${v.vehicle_id}</div>`;
    }).join('');
    document.getElementById('listVehicle').innerHTML = vhHtml || '<div style="padding:10px;color:#999;font-size:11px">' + (variantArr.length||modelArr.length||makeArr.length?'No results':'Select a Variant') + '</div>';
    document.getElementById('countVehicle').textContent = vehicleList.length;

    // Update Select All button label
    const btn = document.getElementById('btnSelectAllVehicles');
    if(btn){
        const allSel = vehicleList.length > 0 && vehicleList.every(v => selectedVehicleIds.has(v.vehicle_id));
        btn.textContent = allSel ? 'None' : 'All';
        btn.style.borderColor = allSel ? 'var(--c)' : 'var(--border)';
        btn.style.color = allSel ? 'var(--c)' : 'var(--text2)';
    }
    
    updateSelectionSummary();
}

function toggleSelectAllVehicles(){
    const searchVehicle = getSearchVal('searchVehicle').toLowerCase();
    const makeArr = Array.from(filterState.makes);
    const modelArr = Array.from(filterState.models);
    const variantArr = Array.from(filterState.variants);

    let vis = allVehicles;
    if(makeArr.length) vis = vis.filter(v => makeArr.includes(v.make));
    if(modelArr.length) vis = vis.filter(v => modelArr.includes(v.model));
    if(variantArr.length) vis = vis.filter(v => variantArr.includes(v.variant));
    const vehicleList = vis.filter(v => !searchVehicle || v.vehicle_id.toLowerCase().includes(searchVehicle));

    const allSelected = vehicleList.every(v => selectedVehicleIds.has(v.vehicle_id));
    if(allSelected){
        vehicleList.forEach(v => selectedVehicleIds.delete(v.vehicle_id));
    } else {
        vehicleList.forEach(v => selectedVehicleIds.add(v.vehicle_id));
    }
    renderFilterLists();
}

function toggleFilter(type, val){
    const s = type==='make'?filterState.makes:(type==='model'?filterState.models:filterState.variants);
    if(s.has(val)) s.delete(val);
    else s.add(val);
    renderFilterLists();
}

function toggleVehicle(id){
    if(selectedVehicleIds.has(id)) selectedVehicleIds.delete(id);
    else selectedVehicleIds.add(id);
    renderFilterLists(); 
}

function clearFilters(){
    filterState.makes.clear();
    filterState.models.clear();
    filterState.variants.clear();
    selectedVehicleIds.clear();
    // Clear search boxes
    ['searchMake','searchModel','searchVariant','searchVehicle'].forEach(id=>{
        const el=document.getElementById(id);
        if(el) el.value='';
    });
    renderFilterLists();
}

function updateSelectionSummary(){
    document.getElementById('selectionSummary').textContent = `${selectedVehicleIds.size} vehicles selected`;
}

function openFilterModal(){
    document.getElementById('filterModal').classList.add('show');
    if(allVehicles.length === 0) loadVehicleFilters();
}

function closeFilterModal(){
    document.getElementById('filterModal').classList.remove('show');
    document.getElementById('btnFilter').textContent = `Filter Vehicles (${selectedVehicleIds.size})`;
}

function applyFilters(){
    closeFilterModal();
}

async function runAnalysis(){
    const btn=document.getElementById('btnFetch'),loader=document.getElementById('loader');
    const payload={
        start_date:document.getElementById('startDate').value,
        end_date:document.getElementById('endDate').value,
        start_time:document.getElementById('startTime').value,
        end_time:document.getElementById('endTime').value,
        vehicle_ids: Array.from(selectedVehicleIds)
    };
    
    if(!payload.start_date||!payload.end_date){showToast('Please select dates',true);return}
    if(selectedVehicleIds.size === 0){ showToast('Please select at least one vehicle', true); return; }

    btn.disabled=true; loader.classList.add('show');
    try{
        const res=await fetch('/api/fetch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        const result=await res.json();
        if(!res.ok) throw new Error(result.message||'Fetch failed');
        document.getElementById('hTotalVehicles').textContent=result.total_eligible||0;
        days=[]; selectAll=true; selected={}; nextId=0; leaderboardSelection.clear(); // Clear manual selection on new analysis
        // Capture both API failures AND parse failures (vehicles with no usable score data)
        const allFailed = [...result.failed];
        result.success.forEach(item=>{
            const d=parseJsonToDay(item.data,item.vehicle_id);
            if(d) days.push(d);
            else allFailed.push({vehicle_id:item.vehicle_id, reason:'No score/metrics data in API response'});
        });
        const fl=document.getElementById('failedList');
        if(allFailed.length){
            fl.innerHTML = allFailed.map((f,i)=>{
                const ddId='fdd_'+i;
                const vid=f.vehicle_id||'Unknown';
                const reason=f.reason||'Unknown error';
                return '<div class="ir" onclick="toggleDrillDown(this)" style="padding:4px 6px;margin-bottom:2px;border-radius:4px;background:var(--bg3);border-left:2px solid var(--cr);cursor:pointer">'
                    +'<span class="ir-l" style="color:var(--text2);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:75%">'+vid+' <span class="ir-icon" style="font-size:9px">&#9660;</span></span>'
                    +'<span style="font-size:9px;color:var(--cr);font-family:JetBrains Mono,monospace;font-weight:600">ERR</span>'
                    +'</div>'
                    +'<div id="'+ddId+'" class="drill-down" style="margin-top:-2px;margin-bottom:4px">'
                    +'<div class="drill-down-row"><span>Vehicle ID</span><span class="drill-down-v" style="color:var(--text);word-break:break-all;font-size:10px">'+vid+'</span></div>'
                    +'<div class="drill-down-row"><span>Reason</span><span class="drill-down-v" style="color:var(--cr);word-break:break-all;white-space:normal;text-align:left;font-size:10px">'+reason+'</span></div>'
                    +'</div>';
            }).join('');
        } else {
            fl.innerHTML='<div style="font-size:10px;color:var(--text3);padding:4px">All vehicles fetched successfully</div>';
        }
        document.getElementById('hFileCount').textContent=days.length;
        refresh();
        showToast('Analysis Complete: '+days.length+' vehicles loaded');
    }catch(e){console.error(e);showToast(e.message,true)}
    finally{btn.disabled=false;loader.classList.remove('show')}
}

function t2s(s){if(!s)return 0;const p=s.split(':').map(Number);return(p[0]*3600+(p[1]||0)*60+(p[2]||0))}
function s2t(s){s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;return`${h}h:${m}m:${sec}s`}
function getSelIds(){return selectAll?days.map(d=>d._id):Object.keys(selected).map(Number)}
function set(id,val){const e=document.getElementById(id);if(e)e.textContent=val}
function showToast(msg,err){const t=document.getElementById('toast');t.textContent=msg;t.className=`toast${err?' err':''} show`;setTimeout(()=>t.classList.remove('show'),5000)}
function scColor(v,max){const r=v/(max||5);return r>=0.8?'var(--cg)':r>=0.5?'var(--co)':'var(--cr)'}
function animArc(id,score,max){
    const arc=document.getElementById(id);if(!arc)return;
    const c=2*Math.PI*25,vis=Math.min(score,max);
    arc.style.strokeDasharray=`${(c*vis/max).toFixed(1)} ${c.toFixed(1)}`;
}

function toggleDrillDown(row){
    row.classList.toggle('expanded');
    const dd=row.nextElementSibling;
    if(dd && dd.classList.contains('drill-down')){
        dd.style.display=dd.style.display==='block'?'none':'block';
    }
}

function renderFileList(){
    const el=document.getElementById('fileList');
    if(!days.length){el.innerHTML='<div style="padding:20px;color:var(--text3);font-size:11px;text-align:center">No files loaded</div>';return}
    const ss={};getSelIds().forEach(id=>ss[id]=true);
    let h=`<button class="fbtn fbtn-all${selectAll?' sel':''}" data-action="all"><span class="fbtn-name">All Vehicles</span><span class="fbtn-score">${days.length}</span></button>`;
    days.forEach(d=>{
        const sel=!!ss[d._id];
        h+=`<button class="fbtn${sel?' sel':''}" data-action="day" data-id="${d._id}"><span class="fbtn-name">${d.date_label}</span><span class="fbtn-score">D:${d.driver_score.toFixed(1)}</span></button>`;
    });
    el.innerHTML=h;
    el.querySelectorAll('.fbtn').forEach(btn=>{
        btn.addEventListener('click',function(e){
            if(this.getAttribute('data-action')==='all'){selectAll=true;selected={}; leaderboardSelection.clear();}
            else{
                const id=Number(this.getAttribute('data-id'));
                if(e.ctrlKey||e.metaKey){selectAll=false;if(selected[id])delete selected[id];else selected[id]=true;if(!Object.keys(selected).length)selectAll=true}
                else{selectAll=false;selected={};selected[id]=true; leaderboardSelection.clear();} // Clear manual selection on single click
            }
            
            // NEW: Auto-switch to compare view if 2 or 3 are selected
            const selectedCount = Object.keys(selected).length;
            if (selectedCount === 2 || selectedCount === 3) {
                viewMode = 'cmp';
            } else if (selectedCount === 1) {
                viewMode = 'agg';
            }
            
            refresh();
        });
    });
    document.getElementById('clearBtn').onclick=()=>{days=[];selected={};selectAll=true;leaderboardSelection.clear();refresh();showToast('Cleared');};
}

// NEW: Toggle function for the + button in leaderboards
function toggleLeaderboardItem(id) {
    if (leaderboardSelection.has(id)) {
        leaderboardSelection.delete(id);
    } else {
        if (leaderboardSelection.size >= 3) {
            showToast('Max 3 vehicles allowed for comparison', true);
            return;
        }
        leaderboardSelection.add(id);
    }
    refresh(); // Re-render to update button states and footer button
}

// NEW: Function to handle deep drill down from event lists
function drillToVehicle(label) {
    const target = days.find(d => d.date_label === label);
    if (target) {
        selectSingleVehicle(target._id);
    }
}

function renderLeaderboards(sel,type){
    const key=type+'_score';
    const sorted=[...sel].sort((a,b)=>b[key]-a[key]);
    const top3=sorted.slice(0,3),bot3=sorted.slice(-3).reverse();

    function mkRows(arr,col){
        const ids=arr.map(d=>d._id);
        let html=arr.map(d=>{
            const isSelected = leaderboardSelection.has(d._id);
            return `<div class="leaderboard-row" style="border-left-color:${col}">
                <span title="${d.date_label}">${d.date_label}</span>
                <button class="cmp-add-btn ${isSelected?'active':''}" onclick="event.stopPropagation(); toggleLeaderboardItem(${d._id})" title="Add to Comparison">${isSelected?'&#10003;':'+'}</button>
                <span class="lboard-score">${d[key].toFixed(1)}</span>
            </div>`;
        }).join('');
        
        // If 2 or 3 items are manually selected via +, show Compare Selected button
        // Otherwise, show the default logic (Compare All 3 or View Details)
        let footerHtml = '';
        if (leaderboardSelection.size >= 2) {
            const selIds = Array.from(leaderboardSelection);
            footerHtml = `<button class="btn-compare" onclick="compareVehicleIds([${selIds.join(',')}])">&#9654; Compare Selected (${leaderboardSelection.size})</button>`;
        } else if(arr.length>=2){
            const label=arr.length===3?'Compare All 3':'Compare';
            html+=`<button class="btn-compare" onclick="compareVehicleIds([${ids.join(',')}])">&#9654; ${label}</button>`;
        } else if(arr.length===1){
            html+=`<button class="btn-compare" onclick="selectSingleVehicle(${ids[0]})">&#9654; View Details</button>`;
        }
        
        // Append manual selection footer if it exists and we are at the bottom of a list
        if (leaderboardSelection.size >= 2) {
            html += footerHtml;
        }
        
        return html;
    }

    document.getElementById(`top${type.charAt(0).toUpperCase()+type.slice(1)}List`).innerHTML=mkRows(top3,'var(--cg)');
    document.getElementById(`bot${type.charAt(0).toUpperCase()+type.slice(1)}List`).innerHTML=mkRows(bot3,'var(--cr)');
}

function renderTopWasters(sel){
    const topSel=sel.map(d=>{
        const s=d.stats;
        const total=s.idle_fuel_l+s.overspeed_fuel_l+s.overrev_fuel_l;
        return{_id:d._id,id:d.date_label,total,idle:s.idle_fuel_l,over:s.overspeed_fuel_l,rev:s.overrev_fuel_l};
    }).sort((a,b)=>b.total-a.total).slice(0,2);

    const container=document.getElementById('topWastersList');
    if(!topSel.length||topSel[0].total===0){
        container.innerHTML='<div style="font-size:11px;color:var(--text3)">No waste data</div>';
        return;
    }

    let html=topSel.map((w,i)=>{
        const color=i===0?'var(--cr)':'var(--co)';
        return `
        <div class="top-waster-card" style="border-left-color:${color}" onclick="this.classList.toggle('expanded'); document.getElementById('wbd_${i}').style.display=this.classList.contains('expanded')?'block':'none'">
            <div class="top-waster-id" title="${w.id}">#${i+1} ${w.id}</div>
            <div class="top-waster-detail"><span>Total Wasted</span><span style="color:${color};font-weight:700">${w.total.toFixed(3)} L</span></div>
            <div class="waster-breakdown" id="wbd_${i}">
                <div class="waster-breakdown-row"><span>Idle Waste:</span> <span>${w.idle.toFixed(3)} L</span></div>
                <div class="waster-breakdown-row"><span>Overspeed Waste:</span> <span>${w.over.toFixed(3)} L</span></div>
                <div class="waster-breakdown-row"><span>Overrev Waste:</span> <span>${w.rev.toFixed(3)} L</span></div>
            </div>
        </div>`;
    }).join('');

    // Add compare button if 2 wasters exist
    if(topSel.length===2){
        const ids=topSel.map(w=>w._id);
        html+=`<button class="btn-compare" style="margin-top:10px" onclick="compareVehicleIds([${ids.join(',')}])">&#9654; Compare These 2</button>`;
    } else if(topSel.length===1){
        html+=`<button class="btn-compare" style="margin-top:10px" onclick="selectSingleVehicle(${topSel[0]._id})">&#9654; View Details</button>`;
    }
    container.innerHTML=html;
}

function renderAgg(sel){
    const n=sel.length;
    let totalDist=0,totalFuel=0,sumAvgSpd=0,hB=0,hA=0,hC=0,mB=0,wGear=0,overSpd=0,coast=0,
        idleSec=0,engOnSec=0,engOffSec=0,dataLossSec=0,startCnt=0,stopCnt=0,
        halfClutch=0,idleFuel=0,overspeedFuel=0,overrevFuel=0,milErr=0,maxSpd=0,
        sumDriverScore=0,sumFuelScore=0;
    const gearTotals={},errSet={};
    
    const hbList=[], haList=[], hcList=[], mbList=[];

    let bestEcon={val:-1,file:''},worstEcon={val:9999,file:''};

    sel.forEach(d=>{
        const s=d.stats;
        totalDist+=s.distance_km; totalFuel+=s.total_fuel_l; sumAvgSpd+=s.avg_speed;
        hB+=s.harsh_brake; hA+=s.harsh_acc; hC+=s.harsh_corner; mB+=s.mod_brake;
        wGear+=s.wrong_gear_km; overSpd+=s.overspeed_km; coast+=s.coasting_km;
        idleSec+=t2s(s.idle_time); engOnSec+=t2s(s.engine_on); engOffSec+=t2s(s.engine_off); dataLossSec+=t2s(s.data_loss);
        startCnt+=s.start_count; stopCnt+=s.stop_count;
        halfClutch+=s.half_clutch_km; idleFuel+=s.idle_fuel_l;
        overspeedFuel+=s.overspeed_fuel_l; overrevFuel+=s.overrev_fuel_l; milErr+=s.mil_error_km;
        if(s.max_speed>maxSpd) maxSpd=s.max_speed;
        Object.keys(s.gear_dist).forEach(g=>gearTotals[g]=(gearTotals[g]||0)+parseFloat(s.gear_dist[g]||0));
        s.err_signals.forEach(sig=>errSet[sig]=true);
        sumDriverScore+=d.driver_score; sumFuelScore+=d.fuel_score;
        
        if(s.harsh_brake>0) hbList.push({id:d.date_label, val:s.harsh_brake});
        if(s.harsh_acc>0) haList.push({id:d.date_label, val:s.harsh_acc});
        if(s.harsh_corner>0) hcList.push({id:d.date_label, val:s.harsh_corner});
        if(s.mod_brake>0) mbList.push({id:d.date_label, val:s.mod_brake});

        if(s.fuel_economy>0){
            if(s.fuel_economy>bestEcon.val) bestEcon={val:s.fuel_economy,file:d.date_label};
            if(s.fuel_economy<worstEcon.val) worstEcon={val:s.fuel_economy,file:d.date_label};
        }
    });

    const populateDD=(id,list)=>{
        const el=document.getElementById(id);
        if(!el) return;
        if(list.length===0){el.innerHTML='<div style="font-size:10px;color:var(--text3);font-style:italic">No events recorded</div>'; return}
        el.innerHTML=list.map(item=>`<div class="drill-down-row clickable" onclick="drillToVehicle('${item.id}')"><span>${item.id}</span><span class="drill-down-v">${item.val}</span></div>`).join('');
    };

    populateDD('dd_Harsh_Braking', hbList.sort((a,b)=>b.val-a.val));
    populateDD('dd_Harsh_Acceleration', haList.sort((a,b)=>b.val-a.val));
    populateDD('dd_Harsh_Cornering', hcList.sort((a,b)=>b.val-a.val));
    populateDD('dd_Moderate_Braking', mbList.sort((a,b)=>b.val-a.val));

    const avgSpd=n?sumAvgSpd/n:0;
    const avgEcon=n?(totalFuel>0?totalDist/totalFuel:0):0;
    const avgDriver=n?sumDriverScore/n:0;
    const avgFuel=n?sumFuelScore/n:0;
    const showLeaderboard=n>0 && n===days.length;
    const totalHarsh=hB+hA+hC;
    const totalWaste=idleFuel+overspeedFuel+overrevFuel;
    const wastePct=totalFuel>0?totalWaste/totalFuel*100:0;
    const idleRatio=engOnSec>0?idleSec/engOnSec*100:0;
    const fuelPer100=totalDist>0?totalFuel/totalDist*100:0;
    const maxWaste=Math.max(idleFuel,overspeedFuel,overrevFuel)||1;

    set('kTotalDist',`${totalDist.toFixed(1)} km`);
    set('kTotalFuel',`${totalFuel.toFixed(1)} L`);
    set('kAvgEcon',`${avgEcon.toFixed(1)} km/L`);
    set('topDriverScore',avgDriver.toFixed(1)); set('topFuelScore',avgFuel.toFixed(1));
    set('driverArcLbl',avgDriver.toFixed(1)); set('fuelArcLbl',avgFuel.toFixed(1));
    animArc('driverArc',avgDriver,5); animArc('fuelArc',avgFuel,5);
    const leaderboardSection=document.getElementById('scoreLeaderboardSection');
    if(leaderboardSection) leaderboardSection.style.display=showLeaderboard?'block':'none';
    if(showLeaderboard){
        renderLeaderboards(sel,'driver'); renderLeaderboards(sel,'fuel');
    }
    renderTopWasters(sel);

    set('wValIdle',`${idleFuel.toFixed(3)} L`);
    set('wValOver',`${overspeedFuel.toFixed(3)} L`);
    set('wValRev',`${overrevFuel.toFixed(3)} L`);
    document.getElementById('wBarIdle').style.width=`${(idleFuel/maxWaste*100).toFixed(0)}%`;
    document.getElementById('wBarOver').style.width=`${(overspeedFuel/maxWaste*100).toFixed(0)}%`;
    document.getElementById('wBarRev').style.width=`${(overrevFuel/maxWaste*100).toFixed(0)}%`;
    set('iTotalWaste',`${totalWaste.toFixed(3)} L`);
    set('iWastePct',`(${wastePct.toFixed(1)}%)`);

    set('iFuelPer100',`${fuelPer100.toFixed(2)} L/100km`);
    set('iBestEcon',bestEcon.file?`${bestEcon.val.toFixed(2)} km/L \u2014 ${bestEcon.file}`:'—');
    set('iWorstEcon',worstEcon.file&&worstEcon.val<9998?`${worstEcon.val.toFixed(2)} km/L \u2014 ${worstEcon.file}`:'—');
    set('iIdleRatio',`${idleRatio.toFixed(1)}%`);
    set('iWrongGearPct',`${totalDist>0?(wGear/totalDist*100).toFixed(1):'0'}%`);
    set('iHalfClutchPct',`${totalDist>0?(halfClutch/totalDist*100).toFixed(1):'0'}%`);
    set('iOverPct',`${totalDist>0?(overSpd/totalDist*100).toFixed(2):'0'}%`);
    set('iCoasting',`${coast.toFixed(2)} km`);

    set('iHarshBrake',hB); set('iHarshAcc',hA); set('iHarshCorn',hC);
    set('iModBrake',mB); set('iTotalHarsh',totalHarsh);
    set('iEventsPerKm',`${totalDist>0?(totalHarsh/totalDist*100).toFixed(2):'0'}/100km`);

    set('iMaxSpeed',`${maxSpd.toFixed(0)} km/h`);
    set('iAvgSpd',`${avgSpd.toFixed(1)} km/h`);
    set('iOverSpd',`${overSpd.toFixed(2)} km`);
    const fastCars=sel.filter(d=>d.stats.max_speed>=80).sort((a,b)=>b.stats.max_speed-a.stats.max_speed);
    document.getElementById('highSpeedList').innerHTML=fastCars.length?fastCars.map(d=>`<div class="ritem" style="border-left-color:var(--cr)"><span>${d.date_label}</span><span>${d.stats.max_speed.toFixed(0)} km/h</span></div>`).join(''):'<div style="color:var(--text3);font-size:11px">No vehicles recorded speeds above 80 km/h</div>';

    set('iEngOn',s2t(engOnSec)); set('iEngOff',s2t(engOffSec)); set('iIdleTime',s2t(idleSec));
    set('iDataLoss',s2t(dataLossSec));
    set('iStartCnt',Math.round(startCnt)); set('iStopCnt',Math.round(stopCnt));
    set('iMilError',`${milErr.toFixed(2)} km`);
    const hcDistPct=totalDist>0?(halfClutch/totalDist*100).toFixed(1):'0';
    const wgDistPct=totalDist>0?(wGear/totalDist*100).toFixed(1):'0';
    set('iHalfClutch',`${halfClutch.toFixed(2)} km (${hcDistPct}%)`); set('iWrongGear',`${wGear.toFixed(2)} km (${wgDistPct}%)`);

    const gKeys=Object.keys(gearTotals).sort();
    const gTotal=gKeys.reduce((a,g)=>a+gearTotals[g],0);
    const maxG=Math.max(...Object.values(gearTotals))||1;
    const gCols=['var(--cb)','var(--c)','var(--cg)','var(--cy)','var(--co)','var(--cp)'];
    document.getElementById('gearBars').innerHTML=gKeys.length?gKeys.map((g,i)=>{
        const p=(gearTotals[g]/maxG*100).toFixed(0),gp=gTotal>0?(gearTotals[g]/gTotal*100).toFixed(1):'0';
        return`<div class="gear-row"><span class="gear-lbl">${g.replace('Gear_','G')}</span><div class="gear-track"><div class="gear-fill" style="width:${p}%;background:${gCols[i%gCols.length]}"></div></div><span class="gear-km">${gearTotals[g].toFixed(1)}</span><span class="gear-pct">${gp}%</span></div>`;
    }).join(''):'<div style="color:var(--text3);font-size:12px">No gear data</div>';

    const ek=Object.keys(errSet);
    const es=document.getElementById('errSection');
    if(ek.length){es.style.display='block';document.getElementById('errList').innerHTML=ek.map(k=>`<span class="err-tag">${k.replace(/_/g,' ')}</span>`).join('')}else{es.style.display='none'}
}

function renderComparison(vehicles){
    const cmp=document.getElementById('cmpContent');
    const gridCols = vehicles.length===3 ? 'grid-template-columns:1fr 1fr 1fr' : 'grid-template-columns:1fr 1fr';

    function colHtml(d){
        const s=d.stats;
        const econ=s.total_fuel_l>0?(s.distance_km/s.total_fuel_l).toFixed(1):'—';
        const fuelPer100=s.distance_km>0?(s.total_fuel_l/s.distance_km*100).toFixed(2):'—';
        const totalHarsh=s.harsh_brake+s.harsh_acc+s.harsh_corner;
        const idleSec=t2s(s.idle_time);
        const engOnSec=t2s(s.engine_on);
        const idleRatio=engOnSec>0?(idleSec/engOnSec*100).toFixed(1):'0';
        const totalWaste=s.idle_fuel_l+s.overspeed_fuel_l+s.overrev_fuel_l;
        const wastePct=s.total_fuel_l>0?(totalWaste/s.total_fuel_l*100).toFixed(1):'0';
        const maxW=Math.max(s.idle_fuel_l,s.overspeed_fuel_l,s.overrev_fuel_l)||1;
        const dCol=scColor(d.driver_score,5),fCol=scColor(d.fuel_score,5);
        // Half clutch and wrong gear percentages of total distance
        const hcPct=s.distance_km>0?(s.half_clutch_km/s.distance_km*100).toFixed(1):'0';
        const wgPct=s.distance_km>0?(s.wrong_gear_km/s.distance_km*100).toFixed(1):'0';

        function ir(label,val,cls=''){return`<div class="ir" style="cursor:default"><span class="ir-l">${label}</span><span class="ir-v ${cls}">${val}</span></div>`}
        function wbar(label,val,color,max){
            const pct=(val/max*100).toFixed(0);
            return`<div class="waste-row"><span class="waste-label" style="min-width:90px">${label}</span><div class="waste-bar-track"><div class="waste-bar-fill" style="background:${color};width:${pct}%"></div></div><span class="waste-val">${val.toFixed(3)} L</span></div>`;
        }
        
        let wasteHtml = wbar('Idle',s.idle_fuel_l,'var(--cr)',maxW);
        wasteHtml += wbar('Overspeed',s.overspeed_fuel_l,'var(--co)',maxW);
        wasteHtml += wbar('Overrev',s.overrev_fuel_l,'var(--cy)',maxW);
        wasteHtml += `<div class="waste-row" style="border-bottom:none; margin-bottom:0; background:transparent; padding-top:8px;">
                        <span class="waste-label" style="font-weight:700">Total Waste</span>
                        <span class="waste-val b">${totalWaste.toFixed(3)} L</span>
                        <span class="waste-val" style="margin-left:auto; min-width:auto; padding-left:10px;">(${wastePct}%)</span>
                      </div>`;

        return`
        <div class="cmp-col">
            <div class="cmp-header">${d.date_label}</div>
            <div class="icard">
                <div class="ict">Scores</div>
                <div class="cmp-score-row">
                    <div class="cmp-score-box">
                        <div class="cmp-score-lbl">Driver</div>
                        <div class="cmp-score-val" style="color:${dCol}">${d.driver_score.toFixed(1)}</div>
                        <div style="font-size:10px;color:var(--text3)">/5</div>
                    </div>
                    <div class="cmp-score-box">
                        <div class="cmp-score-lbl">Fuel</div>
                        <div class="cmp-score-val" style="color:${fCol}">${d.fuel_score.toFixed(1)}</div>
                        <div style="font-size:10px;color:var(--text3)">/5</div>
                    </div>
                </div>
            </div>
            <div class="icard">
                <div class="ict"><span class="dot" style="background:var(--c)"></span>Trip Summary</div>
                ${ir('Distance',s.distance_km.toFixed(1)+' km')}
                ${ir('Total Fuel',s.total_fuel_l.toFixed(1)+' L')}
                ${ir('Fuel Economy',econ+' km/L')}
                ${ir('Fuel per 100km',fuelPer100+' L/100km')}
                ${ir('Avg Speed',s.avg_speed.toFixed(1)+' km/h')}
                ${ir('Max Speed',s.max_speed.toFixed(0)+' km/h')}
            </div>
            <div class="icard">
                <div class="ict"><span class="dot" style="background:var(--cr)"></span>Safety Events</div>
                ${ir('Harsh Braking',s.harsh_brake)}
                ${ir('Harsh Acceleration',s.harsh_acc)}
                ${ir('Harsh Cornering',s.harsh_corner)}
                ${ir('Moderate Braking',s.mod_brake)}
                ${ir('Total Harsh',totalHarsh,totalHarsh>5?'b':totalHarsh>2?'w':'g')}
            </div>
            <div class="icard">
                <div class="ict"><span class="dot" style="background:var(--co)"></span>Fuel Waste</div>
                ${wasteHtml}
            </div>
            <div class="icard">
                <div class="ict"><span class="dot" style="background:var(--cb)"></span>Engine &amp; Drivetrain</div>
                ${ir('Engine ON',s2t(engOnSec),'g')}
                ${ir('Idle Duration',s2t(idleSec),'w')}
                ${ir('Idle / Engine ON',idleRatio+'%')}
                ${ir('Wrong Gear',s.wrong_gear_km.toFixed(2)+' km ('+wgPct+'%)','w')}
                ${ir('Half Clutch',s.half_clutch_km.toFixed(2)+' km ('+hcPct+'%)','w')}
                ${ir('Overspeed km',s.overspeed_km.toFixed(2)+' km','w')}
                ${ir('Coasting km',s.coasting_km.toFixed(2)+' km','g')}
                ${ir('MIL Error km',s.mil_error_km.toFixed(2)+' km','w')}
            </div>
        </div>`;
    }

    cmp.innerHTML=`<div class="cmp-wrap" style="${gridCols};height:100%;overflow:hidden;display:grid">${vehicles.map(colHtml).join('')}</div>`;
}

// ── View mode toggle ──
function setViewMode(mode){
    viewMode=mode;
    document.getElementById('vtabAgg').classList.toggle('active', mode==='agg');
    document.getElementById('vtabCmp').classList.toggle('active', mode==='cmp');
    refresh();
    document.getElementById('mainPanel').scrollTop=0;
}

// Compare a specific set of vehicle _ids (2 or 3)
function compareVehicleIds(ids){
    // Save current selection so the × button can restore it
    prevSelection={selectAll, selected:{...selected}};
    selectAll=false;
    selected={};
    ids.forEach(id=>selected[id]=true);
    leaderboardSelection.clear(); // Clear manual selection when entering comparison
    viewMode='cmp';
    refresh();
    // Scroll main panel to top so toggle bar is visible immediately
    document.getElementById('mainPanel').scrollTop=0;
}

// Select a single vehicle for detail view
function selectSingleVehicle(id){
    prevSelection={selectAll, selected:{...selected}};
    selectAll=false;
    selected={};
    selected[id]=true;
    leaderboardSelection.clear();
    viewMode='agg';
    refresh();
    document.getElementById('mainPanel').scrollTop=0;
}

// Close/restore from a compare-button-triggered view
function closeCompareView(){
    if(prevSelection){
        selectAll=prevSelection.selectAll;
        selected=prevSelection.selected;
        prevSelection=null;
    } else {
        selectAll=true;
        selected={};
    }
    leaderboardSelection.clear(); // Clear manual selection on close
    viewMode='agg';
    refresh();
    document.getElementById('mainPanel').scrollTop=0;
}

function refresh(){
    renderFileList();

    const hasData=days.length>0;
    const ids=getSelIds();
    const sel=days.filter(d=>ids.includes(d._id));
    const n=sel.length;

    const mainPanel=document.getElementById('mainPanel');
    mainPanel.style.padding='20px';
    mainPanel.style.overflowY='auto';
    mainPanel.style.display='flex';
    mainPanel.style.flexDirection='column';
    mainPanel.style.gap='16px';

    document.getElementById('emptyState').style.display=hasData?'none':'flex';
    document.getElementById('aggContent').style.display='none';
    document.getElementById('cmpContent').style.display='none';

    if(!hasData) return;

    // Show toggle bar when 2-3 manually selected; show close-only bar when prevSelection exists
    const canToggle = (n===2||n===3) && !selectAll;
    const hasClose = !!prevSelection;
    const toggleBar = document.getElementById('viewToggleBar');
    toggleBar.classList.toggle('show', canToggle || hasClose);
    // Hide agg/cmp tabs when in single-vehicle detail (only close button needed)
    document.getElementById('vtabAgg').style.display = canToggle ? '' : 'none';
    document.getElementById('vtabCmp').style.display = canToggle ? '' : 'none';
    document.getElementById('vtabClose').style.borderLeft = canToggle ? '1px solid var(--border)' : 'none';
    document.getElementById('vtabClose').style.borderRadius = canToggle ? '0 8px 8px 0' : '8px';

    // When toggle bar appears for compare, stay in cmp; reset to agg when leaving
    if(!canToggle && !hasClose) viewMode='agg';

    // Keep tab indicators in sync
    document.getElementById('vtabAgg').classList.toggle('active', viewMode==='agg');
    document.getElementById('vtabCmp').classList.toggle('active', viewMode==='cmp');

    if(canToggle && viewMode==='cmp'){
        // Comparison: flex-col so toggle bar sits on top, cmp fills remaining height
        mainPanel.style.padding='10px 10px 0 10px';
        mainPanel.style.overflowY='hidden';
        mainPanel.style.display='flex';
        mainPanel.style.flexDirection='column';
        mainPanel.style.gap='8px';
        // cmpContent needs to fill remaining height
        const cmpEl=document.getElementById('cmpContent');
        cmpEl.style.flex='1';
        cmpEl.style.minHeight='0';
        cmpEl.style.display='flex';
        renderComparison(sel);
    } else {
        mainPanel.style.padding='20px';
        mainPanel.style.overflowY='auto';
        mainPanel.style.display='flex';
        mainPanel.style.flexDirection='column';
        mainPanel.style.gap='16px';
        const cmpEl=document.getElementById('cmpContent');
        cmpEl.style.flex='';
        cmpEl.style.minHeight='';
        document.getElementById('aggContent').style.display='flex';
        renderAgg(sel);
    }
}

window.addEventListener('DOMContentLoaded',checkSession);
</script>
</body>
</html>
"""

# ── DATABASE LAYER ─────────────────────────────────────────────────────
def get_db_connection():
    if not MYSQL_AVAILABLE: return None
    try:
        config = load_config()
        db_conf = config.get('database', {})
        db_port = db_conf.get('port', 3306)
        try:
            db_port = int(db_port)
        except (TypeError, ValueError):
            db_port = 3306
        conn = mysql.connector.connect(
            host=db_conf.get('host', 'localhost'),
            port=db_port,
            user=db_conf.get('user', 'root'),
            password=db_conf.get('password', ''),
            database=db_conf.get('database', 'fleet_analytics')
        )
        return conn
    except Error as e:
        logging.error(f"Database Connection Error: {e}")
        return None

def setup_database_tables():
    conn = get_db_connection()
    if not conn: return
    cursor = conn.cursor()
    try:
        cursor.execute("CREATE TABLE IF NOT EXISTS makes (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100) NOT NULL UNIQUE)")
        cursor.execute("CREATE TABLE IF NOT EXISTS models (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100) NOT NULL, make_id INT NOT NULL, UNIQUE KEY unique_model_per_make (name, make_id), FOREIGN KEY (make_id) REFERENCES makes(id) ON DELETE CASCADE)")
        cursor.execute("CREATE TABLE IF NOT EXISTS variants (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100) NOT NULL, model_id INT NOT NULL, UNIQUE KEY unique_variant_per_model (name, model_id), FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE)")
        cursor.execute("CREATE TABLE IF NOT EXISTS vehicles (id INT AUTO_INCREMENT PRIMARY KEY, vehicle_id VARCHAR(100) NOT NULL UNIQUE, variant_id INT NOT NULL, sub_start_time DATETIME, FOREIGN KEY (variant_id) REFERENCES variants(id) ON DELETE CASCADE)")
        conn.commit()
        logging.info("Database tables verified.")
    except Error as e:
        logging.error(f"Error verifying tables: {e}")
    finally:
        cursor.close()
        conn.close()

VALID_MAKES = ["SML"]
CUTOFF_DATE = datetime(2025, 5, 9, tzinfo=timezone.utc)

def sync_vehicles_to_db():
    if not MYSQL_AVAILABLE: return
    token = get_access_token()
    if not token:
        logging.error("Cannot sync vehicles: Auth failed.")
        return

    config = load_config()

    registry_config = config.get('vehicle_registry', {})
    base_url = registry_config.get('base_url')
    endpoint = registry_config.get('endpoint')
    if not base_url or not endpoint: return

    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    headers = {'Authorization': f'Bearer {token}'}
    
    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
        
        raw_list = []
        
        if isinstance(data, dict):
            if 'data' in data and isinstance(data['data'], dict):
                if 'subscriptions' in data['data'] and isinstance(data['data']['subscriptions'], list):
                    raw_list = data['data']['subscriptions']
                    logging.info("Found vehicle list in response.data.subscriptions")
            
            if not raw_list:
                 if isinstance(data.get('data'), list):
                     raw_list = data['data']
                     logging.info("Found vehicle list in response.data (direct list)")
                 elif isinstance(data.get('subscriptions'), list):
                     raw_list = data['subscriptions']
                     logging.info("Found vehicle list in response.subscriptions (direct list)")
        
        elif isinstance(data, list):
            raw_list = data
            logging.info("API returned a direct list.")
        else:
            logging.warning(f"API returned unexpected type: {type(data)}")

        if not isinstance(raw_list, list):
            logging.error(f"Could not extract a list from API response. Response structure: {str(data)[:200]}")
            return

        if len(raw_list) > 0:
            logging.info(f"Sample item from API (first 500 chars): {str(raw_list[0])[:500]}")
        else:
            logging.warning("API returned an empty list.")

        vehicles_to_sync = []
        
        for item in raw_list:
            if not isinstance(item, dict):
                logging.warning(f"Skipping non-dict item in list: {item}")
                continue

            if 'vehicle' in item and isinstance(item['vehicle'], dict):
                vehicle_obj = item['vehicle']
                sub_str = item.get('subscriptionStartTime')
            else:
                vehicle_obj = item
                sub_str = item.get('subscriptionStartTime')

            v_id = (vehicle_obj.get('vehicleId') or 
                     vehicle_obj.get('vehicle_id') or 
                     vehicle_obj.get('id') or 
                     vehicle_obj.get('uuid'))

            if v_id:
                v_id = str(v_id)

            make_raw = vehicle_obj.get('make', '')
            if make_raw:
                make = str(make_raw).upper()
            else:
                make = ""

            model = str(vehicle_obj.get('model', 'Unknown'))
            variant = str(vehicle_obj.get('variant', 'Unknown'))

            sub_date = None
            if sub_str:
                try:
                    clean_date_str = sub_str.replace('Z', '+00:00')
                    sub_date = datetime.fromisoformat(clean_date_str)
                    if sub_date.tzinfo is None: 
                        sub_date = sub_date.replace(tzinfo=timezone.utc)
                except Exception as e:
                    logging.warning(f"Failed to parse date '{sub_str}': {e}")
                    continue
            else:
                continue
            
            if VALID_MAKES and make not in VALID_MAKES:
                logging.info(f"Skipping {v_id}: Make is '{make}' (Required: {VALID_MAKES})")
                continue

            if sub_date <= CUTOFF_DATE:
                logging.info(f"Skipping {v_id}: Date {sub_date} is before cutoff {CUTOFF_DATE}")
                continue
            
            vehicles_to_sync.append({
                'id': v_id,
                'make': make,
                'model': model,
                'variant': variant,
                'sub_time': sub_date
            })

        logging.info(f"Total items processed: {len(raw_list)}. Eligible Vehicles (After Filter): {len(vehicles_to_sync)}")

        conn = get_db_connection()
        if not conn: return
        
        conn.autocommit = True
        cursor = conn.cursor(dictionary=True)
        
        make_cache = {}
        model_cache = {}
        variant_cache = {}
        
        logging.info("Loading existing DB data into cache...")
        cursor.execute("SELECT id, name FROM makes")
        for row in cursor.fetchall(): make_cache[row['name']] = row['id']
        
        cursor.execute("SELECT id, name, make_id FROM models")
        for row in cursor.fetchall(): model_cache[f"{row['make_id']}_{row['name']}"] = row['id']
        
        cursor.execute("SELECT id, name, model_id FROM variants")
        for row in cursor.fetchall(): variant_cache[f"{row['model_id']}_{row['name']}"] = row['id']

        sync_count = 0
        
        for v in vehicles_to_sync:
            make_id = make_cache.get(v['make'])
            if not make_id:
                cursor.execute("INSERT INTO makes (name) VALUES (%s)", (v['make'],))
                make_id = cursor.lastrowid
                make_cache[v['make']] = make_id
            
            m_key = f"{make_id}_{v['model']}"
            model_id = model_cache.get(m_key)
            if not model_id:
                cursor.execute("INSERT INTO models (name, make_id) VALUES (%s, %s)", (v['model'], make_id))
                model_id = cursor.lastrowid
                model_cache[m_key] = model_id
            
            v_key = f"{model_id}_{v['variant']}"
            variant_id = variant_cache.get(v_key)
            if not variant_id:
                cursor.execute("INSERT INTO variants (name, model_id) VALUES (%s, %s)", (v['variant'], model_id))
                variant_id = cursor.lastrowid
                variant_cache[v_key] = variant_id

            cursor.execute("""
                INSERT INTO vehicles (vehicle_id, variant_id, sub_start_time)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                variant_id = VALUES(variant_id),
                sub_start_time = VALUES(sub_start_time)
            """, (v['id'], variant_id, v['sub_time']))
            
            sync_count += 1

        logging.info(f"Vehicle sync complete. Processed {sync_count} vehicles.")
    except Exception as e:
        logging.error(f"Sync Error: {e}", exc_info=True)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

def get_vehicle_filters_from_db():
    if not MYSQL_AVAILABLE: return []
    conn = get_db_connection()
    if not conn: return []
    try:
        cursor = conn.cursor(dictionary=True)
        query = """
        SELECT v.vehicle_id, ma.name as make, mo.name as model, va.name as variant
        FROM vehicles v
        JOIN variants va ON v.variant_id = va.id
        JOIN models mo ON va.model_id = mo.id
        JOIN makes ma ON mo.make_id = ma.id
        ORDER BY ma.name, mo.name, va.name
        """
        cursor.execute(query)
        results = cursor.fetchall()
        return [
            {
                'vehicle_id': r['vehicle_id'],
                'make': r['make'],
                'model': r['model'],
                'variant': r['variant']
            } for r in results
        ]
    except Exception as e:
        logging.error(f"DB Filter Error: {e}")
        return []
    finally:
        if conn.is_connected(): conn.close()

# ── AUTH & UTILS ─────────────────────────────────────────────────────
def get_access_token(force_refresh=False):
    global AUTH_TOKEN_CACHE
    now = datetime.now(timezone.utc).timestamp()
    # Return cached token if still valid and not forced to refresh
    if not force_refresh and "token" in AUTH_TOKEN_CACHE:
        if now < AUTH_TOKEN_CACHE.get("expires_at", 0):
            return AUTH_TOKEN_CACHE["token"]
        else:
            logging.info("Auth token expired, refreshing...")
    try:
        config = load_config()
        auth_config=config.get('auth',{})
        url=f"{auth_config['base_url'].rstrip('/')}/{auth_config['endpoint'].lstrip('/')}"
        payload={"clientId":auth_config['client_id'],"clientSecret":auth_config['client_secret']}
        response=requests.post(url,json=payload,timeout=10)
        response.raise_for_status()
        token_data=response.json().get('data',{})
        access_token=token_data.get('accessToken')
        if access_token:
            AUTH_TOKEN_CACHE["token"]=access_token
            AUTH_TOKEN_CACHE["expires_at"]=now + TOKEN_EXPIRY_SECONDS
            logging.info("Auth token refreshed successfully.")
            return access_token
        return None
    except Exception as e:
        logging.error("Auth Error: %s",e)
        return None

def process_vehicle(v_id,config,token,from_date,to_date):
    pred_config=config.get('prediction_service',{})
    decrypt_config=config.get('decryption_service',{})
    pred_base_url=pred_config.get('base_url')
    pred_template=pred_config.get('url_template')
    decrypt_base_url=decrypt_config.get('base_url')
    decrypt_endpoint=decrypt_config.get('endpoint')
    path_only=pred_template.split('?')[0]
    base_request_url=f"{pred_base_url.rstrip('/')}/{path_only.lstrip('/')}".format(id=v_id)
    decrypt_url=f"{decrypt_base_url.rstrip('/')}/{decrypt_endpoint.lstrip('/')}"
    query_params={"from":from_date,"to":to_date}

    def _attempt(auth_token):
        headers={'Authorization':f'Bearer {auth_token}','User-Agent':'PostmanRuntime/7.32.3','Accept':'*/*'}
        pred_response=requests.get(base_request_url,headers=headers,params=query_params,timeout=15)
        return pred_response

    try:
        pred_response = _attempt(token)

        # On 400/401/403 the token likely expired mid-batch — force refresh and retry once
        if pred_response.status_code in (400, 401, 403):
            logging.warning(f"Token rejected (HTTP {pred_response.status_code}) for {v_id}, refreshing token and retrying...")
            fresh_token = get_access_token(force_refresh=True)
            if fresh_token:
                pred_response = _attempt(fresh_token)
            else:
                return{"vehicle_id":v_id,"status":"failed","reason":"Token refresh failed"}

        if pred_response.status_code==200:
            pred_json=pred_response.json()
            encrypted_data=pred_json.get('Data')
            if encrypted_data:
                decrypt_payload={"clientId":decrypt_config.get('client_id'),"clientSecret":decrypt_config.get('client_secret'),"data":encrypted_data}
                decrypt_resp=requests.post(decrypt_url,json=decrypt_payload,timeout=10)
                if decrypt_resp.status_code==200:
                    raw=decrypt_resp.json()
                    data_payload=raw if isinstance(raw,str) else (raw.get('Data') or raw.get('data') if isinstance(raw,dict) else raw)
                    final_content=raw
                    if data_payload:
                        if isinstance(data_payload,str):
                            try: final_content=json.loads(data_payload)
                            except json.JSONDecodeError:
                                try: final_content=ast.literal_eval(data_payload)
                                except: final_content=data_payload
                        elif isinstance(data_payload,dict): final_content=data_payload
                    return{"vehicle_id":v_id,"status":"success","data":final_content}
                else: return{"vehicle_id":v_id,"status":"failed","reason":f"Decryption Failed: {decrypt_resp.text[:50]}"}
            else: return{"vehicle_id":v_id,"status":"failed","reason":"No 'Data' field found"}
        else: return{"vehicle_id":v_id,"status":"failed","reason":f"Pred API {pred_response.status_code}"}
    except Exception as e:
        return{"vehicle_id":v_id,"status":"failed","reason":str(e)[:30]}

def fetch_and_process_data(params):
    global DATA_STORE
    DATA_STORE={"success":[],"failed":[],"total_eligible":0}
    token=get_access_token()
    if not token: return{"error":"Auth Failed"}
    config = load_config()

    s_date=datetime.strptime(params['start_date'],'%Y-%m-%d')
    e_date=datetime.strptime(params['end_date'],'%Y-%m-%d')
    s_time=datetime.strptime(params['start_time'],'%H:%M').time()
    e_time=datetime.strptime(params['end_time'],'%H:%M').time()
    start_dt=datetime.combine(s_date.date(),s_time)
    end_dt=datetime.combine(e_date.date(),e_time)
    from_date=start_dt.strftime('%Y-%m-%d %H:%M:%S')
    to_date=end_dt.strftime('%Y-%m-%d %H:%M:%S')

    vehicle_ids = params.get('vehicle_ids', [])
    
    if not vehicle_ids and MYSQL_AVAILABLE:
        logging.info("No IDs provided, defaulting to all eligible vehicles from DB.")
        vehicles = get_vehicle_filters_from_db()
        vehicle_ids = [v['vehicle_id'] for v in vehicles]

    if not vehicle_ids: return {"message":"No vehicles selected"}

    DATA_STORE['total_eligible'] = len(vehicle_ids)
    logging.info(f"Fetching {len(vehicle_ids)} vehicles: {from_date} → {to_date}")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures={executor.submit(process_vehicle,v_id,config,token,from_date,to_date):v_id for v_id in vehicle_ids}
        for future in as_completed(futures):
            result=future.result()
            with DATA_LOCK:
                if result['status']=='success':
                    DATA_STORE["success"].append({"vehicle_id":result['vehicle_id'],"data":result['data']})
                else:
                    DATA_STORE["failed"].append({"vehicle_id":result['vehicle_id'],"reason":result['reason']})

    logging.info(f"Done. Success:{len(DATA_STORE['success'])} Failed:{len(DATA_STORE['failed'])}")
    return{"status":"ok"}

def get_session_id():
    return ''.join(random.choices(string.ascii_letters+string.digits,k=32))

class FleetHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path=='/':
            self.send_response(200); self.send_header('Content-type','text/html'); self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode('utf-8'))
        elif self.path=='/health':
            self.send_response(200); self.send_header('Content-type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({"status":"ok"}).encode())
        elif self.path=='/api/check-session':
            ok=self._check_session()
            self.send_response(200); self.send_header('Content-type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({"logged_in":ok}).encode())
        elif self.path == '/api/vehicles/filters':
            if not self._check_session():
                self.send_response(403); self.end_headers(); return
            data = get_vehicle_filters_from_db()
            self.send_response(200); self.send_header('Content-type','application/json'); self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        else: super().do_GET()

    def do_POST(self):
        if self.path=='/api/login':
            data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            try:
                config = load_config()
                auth_config=config.get('auth',{})
                client_id=data.get('client_id','')
                client_secret=data.get('client_secret','')
                # Attempt auth token generation with provided credentials
                url=f"{auth_config['base_url'].rstrip('/')}/{auth_config['endpoint'].lstrip('/')}"
                payload={"clientId":client_id,"clientSecret":client_secret}
                resp=requests.post(url,json=payload,timeout=10)
                if resp.status_code==200:
                    token_data=resp.json().get('data',{})
                    access_token=token_data.get('accessToken')
                    if access_token:
                        # Cache the token immediately so subsequent API calls use it
                        now=datetime.now(timezone.utc).timestamp()
                        AUTH_TOKEN_CACHE['token']=access_token
                        AUTH_TOKEN_CACHE['expires_at']=now+TOKEN_EXPIRY_SECONDS
                        logging.info("Login: Auth token acquired and cached.")
                        sid=get_session_id()
                        with SESSION_LOCK: SESSIONS[sid]=True
                        self.send_response(200); self.send_header('Content-type','application/json')
                        self.send_header('Set-Cookie',f'session_id={sid}; Path=/; HttpOnly'); self.end_headers()
                        self.wfile.write(json.dumps({"success":True}).encode())
                    else:
                        self.send_response(200); self.end_headers()
                        self.wfile.write(json.dumps({"success":False,"message":"No token in response"}).encode())
                else:
                    msg=f"Auth API returned {resp.status_code}"
                    try: msg=resp.json().get('message',msg)
                    except: pass
                    self.send_response(200); self.end_headers()
                    self.wfile.write(json.dumps({"success":False,"message":msg}).encode())
            except Exception as e:
                logging.error("Login error: %s",e)
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({"success":False,"message":"Server error: "+str(e)[:60]}).encode())

        elif self.path=='/api/logout':
            cookie=self.headers.get('Cookie','')
            for c in cookie.split(';'):
                if 'session_id=' in c:
                    sid=c.strip().split('=')[1]
                    with SESSION_LOCK:
                        if sid in SESSIONS: del SESSIONS[sid]
            self.send_response(200); self.end_headers()

        elif self.path=='/api/fetch':
            if not self._check_session():
                self.send_response(403); self.end_headers(); return
            params=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            try:
                fetch_and_process_data(params)
                resp={"total_eligible":DATA_STORE['total_eligible'],"success":DATA_STORE['success'],"failed":DATA_STORE['failed']}
                self.send_response(200); self.send_header('content-type','application/json'); self.end_headers()
                self.wfile.write(json.dumps(resp).encode())
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(json.dumps({"error":str(e)}).encode())
        
        elif self.path == '/api/vehicles/sync':
            if not self._check_session():
                self.send_response(403); self.end_headers(); return
            threading.Thread(target=sync_vehicles_to_db, daemon=True).start()
            self.send_response(200); self.send_header('Content-type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({"status": "syncing"}).encode())

    def _check_session(self):
        cookie=self.headers.get('Cookie','')
        for c in cookie.split(';'):
            if 'session_id=' in c:
                sid=c.strip().split('=')[1]
                if sid in SESSIONS: return True
        return False

    def log_message(self,format,*args):
        if 'favicon.ico' not in str(args): super().log_message(format,*args)


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

def start_server():
    if MYSQL_AVAILABLE:
        setup_database_tables()
        threading.Thread(target=sync_vehicles_to_db, daemon=True).start()
    else:
        logging.warning("Starting without database features.")

    with ThreadingTCPServer((HOST, PORT), FleetHandler) as httpd:
        logging.info("Serving at http://%s:%s", HOST, PORT)
        httpd.serve_forever()

if __name__=='__main__':
    try:
        start_server()
    except KeyboardInterrupt:
        logging.info("Stopping server...")