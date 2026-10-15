from __future__ import annotations
import asyncio, inspect, json, re, socket, secrets, threading, webbrowser, ipaddress, os, io, contextlib, traceback
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
from html import escape
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import nitrogen
Page = nitrogen.require('f').Page
Script = nitrogen.require('f.scripting').Script
Element = nitrogen.require('f.structuring').Element
RawHTML = nitrogen.require('f.structuring').RawHTML
Color = nitrogen.require('mg.color').Color


ON_PRESS, ON_RELEASE, ON_HOLD = 'press', 'release', 'hold'
THEME_COLOR = Color.light_coral
_KEY = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*$')

def _lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('192.0.2.1', 80))
            return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'

def _loopback(request):
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        return False

def _page_parts(html):
    """Extract generated Fluorine head and body markup."""
    head = re.search(r'<head\b[^>]*>(.*?)</head\s*>', html, re.I | re.S)
    body = re.search(r'<body\b[^>]*>(.*?)</body\s*>', html, re.I | re.S)
    if body is None:
        raise ValueError('Fluorine did not generate a body element')
    # Head contents may include a title and styles/scripts specific to the page.
    return (head.group(1) if head else ''), body.group(1)

def apply_styling(page):
    page.style('body',
        display='flex',
        justify_content='center',
        align_items='center',
        background_color='#111',
        width='100%',
        height='100%',
        margin=0,
        overflow='hidden',
        overscroll_behavior='none',
        touch_action='none',
        color='white',
        font_family='Arial, sans-serif',
    )
    page.style('button',
        width='90%',
        background_color='#222',
        border='none',
        border_radius='0',
        box_shadow='none',
        padding='8px 16px',
        margin='1em 0',
        cursor='pointer',
        font_size='3em',
        color='white'
    )
    return page

class Client:
    def __init__(self, host, client_id, token, group):
        self.host = host
        self.id = client_id
        self._token = token
        if group is None:
            raise ValueError("Client requires a group")
        self.group = group
        self.full_id = f'{group.id}:{client_id}'
        self.name = 'Host' if client_id == 'HOST' else f'Client {client_id}'
        self.status = 'active'
        self.reason = ''
        self._sockets = set()
        self._online = False
        self.device_type = "Unknown"
        self.user_agent = ""
        self._presented_page = None

    def present(self, page):
        """Override this client's page; None restores group/host inheritance."""
        self.host._validate_page(page)
        self._presented_page = page
        self.host._present_changed(self)
        return self

    def online(self):
        return bool(self._sockets)

    def offline(self):
        return not self.online()

    def rename(self, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('Name must be 1-80 characters')
        self.name = name.strip()
        self.host._refresh_admin()
        return self

    def pause(self, reason='Session paused'):
        if self.status != 'active':
            raise ValueError('Only active clients can be paused')
        self.status, self.reason = 'paused', str(reason)
        self.host._state_changed(self)
        return self

    def resume(self):
        if self.status != 'paused':
            raise ValueError('Only paused clients can be resumed')
        self.status, self.reason = 'active', ''
        self.host._state_changed(self)
        return self

    def kick(self, reason='Removed by host'):
        if self.id == 'HOST':
            raise ValueError('Cannot kick the host')
        self.status, self.reason = 'kicked', str(reason)
        self.host._state_changed(self)
        return self

    # IP bans are managed by Group.ban(), never by individual sessions.

    def send(self, name, data=None):
        return self.host.send(self, name, data)

    def update(self, selector, **kwargs):
        return self.host.update(selector, client=self, **kwargs)

    def notify(self, message, title='Notice', level='info', duration=10000):
        return self.host.notify(message, title=title, level=level, duration=duration, client=self)
    
class Group:
    """All browser sessions sharing a peer IP. IDs are stable for this host run."""
    def __init__(self, host, group_id, ip):
        self.host, self.id, self.ip = host, group_id, ip
        self.name = f'Group {group_id}'
        self._items = []
        self.status, self.reason = 'active', ''
        self._presented_page = None

    def __getitem__(self, index): return self._items[index]
    def __iter__(self): return iter(self._items)
    def __len__(self): return len(self._items)
    def online(self): return any(c.online() for c in self._items)
    def offline(self): return not self.online()

    def present(self, page):
        """Set a group-wide page; clients with overrides retain their page."""
        self.host._validate_page(page)
        self._presented_page = page
        self.host._present_changed(self)
        return self

    def rename(self, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('Name must be 1-80 characters')
        self.name = name.strip()
        self.host._refresh_admin()
        return self

    def pause(self, reason='Group paused'):
        if self.status != 'active': raise ValueError('Group is not active')
        self.status, self.reason = 'paused', str(reason)
        self.host._group_state_changed(self)
        return self

    def resume(self):
        if self.status != 'paused': raise ValueError('Group is not paused')
        self.status, self.reason = 'active', ''
        self.host._group_state_changed(self)
        return self

    def kick(self, reason='Group kicked'):
        self.status, self.reason = 'kicked', str(reason)
        self.host._group_state_changed(self)
        return self

    def ban(self, reason='Group banned'):
        self.host._bans[self.ip] = str(reason)
        self.host._save_bans()
        self.status, self.reason = 'banned', str(reason)
        self.host._group_state_changed(self)
        return self

    def notify(self, message, title='Notice', level='info', duration=10000):
        return self.host._submit(self.host._send_packet(
            {'type':'notification','message':str(message),'title':title,'level':level,'duration':duration}, self))

class ClientGroups:
    def __init__(self, host):
        self._host = host
        self._items = []
        self._by_ip = {}
        self._by_token = {}
        self._by_tab = {}  # (peer IP, browser-tab ID) -> Client
        self._host_group = Group(host, 'HOST', '127.0.0.1')
        self._host_client = Client(host, 'HOST', secrets.token_urlsafe(32), self._host_group)
        self._host_group._items.append(self._host_client)

    def __getitem__(self, index): return self._items[index]
    def __iter__(self): return iter(self._items)
    def __len__(self): return len(self._items)
    def get_host(self): return self._host_client
    def by_ip(self, ip): return self._by_ip.get(ip)
    def unban(self, ip):
        self._host._bans.pop(ip, None)
        self._host._save_bans()
        group = self._by_ip.get(ip)
        if group and group.status == 'banned':
            group.status, group.reason = 'active', ''
            self._host._group_state_changed(group)

    def _resolve(self, token, ip, create=True):
        # Host privileges are granted only by the loopback-only host cookie.
        group = self._by_ip.get(ip)
        if group and group.status in ('kicked','banned'): return None
        client = self._by_token.get(token)
        if client is not None and client.group.ip == ip: return client
        if not create or not ip or ip in self._host._bans: return None
        if group is None:
            if self._host.max_clients is not None and sum(
                g.status not in ('kicked','banned') for g in self._items) >= self._host.max_clients:
                return None
            group = Group(self._host, len(self._items), ip)
            self._items.append(group)
            self._by_ip[ip] = group
        client = Client(self._host, len(group._items), secrets.token_urlsafe(32), group)
        group._items.append(client)
        self._by_token[client._token] = client
        self._host._refresh_admin()
        return client


    def _resolve_tab(self, token, ip, tab, create=False):
        """Resolve one browser tab without merging it with sibling tabs sharing cookies."""
        if not isinstance(tab, str) or not re.fullmatch(r'[a-f0-9]{32}', tab):
            return self._resolve(token, ip, create=create)
        # A valid session cookie is required before a tab can claim a session.
        base = self._by_token.get(token)
        if base is None or base.group.ip != ip:
            return None
        if ip in self._host._bans or base.group.status in ('kicked', 'banned'):
            return None
        key = (ip, tab)
        existing = self._by_tab.get(key)
        if existing is not None:
            return existing
        if not create:
            return None
        # The first tab claims the cookie's original client. Further tabs get
        # distinct Client objects and sockets, even though cookies are shared.
        if not any(c is base for c in self._by_tab.values()):
            client = base
        else:
            client = self._resolve(None, ip, create=True)
            if client is None:
                return None
        self._by_tab[key] = client
        return client

# Backwards-compatible registry name, but host.cgroups is the canonical API.
ClientRegistry = ClientGroups

_CLIENT_JS = r'''(() => {
  const listeners = new Map();
  const holds = new Map();
  let ws, retry = 500, connected = false, ready = false;
  const queue = [];
  const pendingNotifications = [];
  const notifyRoot = document.createElement('div');
  notifyRoot.id = 'brlm-notifications';
  const stage = document.createElement('main');
  stage.id = 'brlm-stage';
  let presentedVersion = null;
  function ensureRoots() {
    const parent = document.body || document.documentElement;
    if (!parent) return false;
    if (!stage.isConnected) parent.appendChild(stage);
    if (!notifyRoot.isConnected) parent.appendChild(notifyRoot);
    return true;
  }
  function init() {
    ensureRoots();
    ready = true;
    for (const args of pendingNotifications.splice(0)) show(...args);
    connect();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
  else init();
  function show(message, title='Notice', level='info', duration=10000) {
    if (!ready) { pendingNotifications.push([message,title,level,duration]); return; }
    if (!notifyRoot.isConnected && !ensureRoots()) { pendingNotifications.push([message,title,level,duration]); return; }
    const node = document.createElement('div');
    node.className = 'brlm-notification brlm-' + String(level).replace(/[^a-z-]/g, '');
    const heading = document.createElement('strong'); heading.textContent = title;
    const content = document.createElement('p'); content.textContent = message;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'brlm-notification-close';
    close.textContent = '×';
    close.setAttribute('aria-label', 'Dismiss notification');
    close.onclick = () => node.remove();
    node.append(close, heading, content); notifyRoot.append(node);
    if (duration > 0) setTimeout(() => node.remove(), duration);
    return node;
  }
  function stopAll() { for (const el of holds.keys()) stopHold(el); }
  function stopHold(el) { clearInterval(holds.get(el)); holds.delete(el); }
  function renderState(state) {
    let overlay = document.getElementById('brlm-state');
    if (state.status === 'active') {
      if (overlay) overlay.remove(); return;
    }
    stopAll();
    if (!overlay) { overlay = document.createElement('div'); overlay.id = 'brlm-state'; document.body.append(overlay); }
    overlay.replaceChildren();
    const h = document.createElement('h2'), p = document.createElement('p');
    h.textContent = state.status === 'paused' ? 'Session paused' : state.status === 'kicked' ? 'Session kicked' : 'Session banned';
    p.textContent = state.reason || 'You cannot interact with this host.';
    overlay.append(h, p);
  }
  function activateScripts(root) {
    root.querySelectorAll('script').forEach(old => {
      const script = document.createElement('script');
      for (const attr of old.attributes) script.setAttribute(attr.name, attr.value);
      script.textContent = old.textContent;
      old.replaceWith(script);
    });
  }
  function present(packet) {
    if (!ensureRoots()) return;
    if (presentedVersion === packet.version) return;
    presentedVersion = packet.version;
    stopAll();
    const headTemplate = document.createElement('template');
    headTemplate.innerHTML = packet.head || '';
    // Page-specific head nodes live in the real document head.
    const previous = document.querySelectorAll('[data-brlm-page-head]');
    previous.forEach(node => node.remove());
    for (const node of [...headTemplate.content.childNodes]) {
      if (node.nodeType !== Node.ELEMENT_NODE) continue;
      node.setAttribute('data-brlm-page-head', '');
      document.head.appendChild(node);
    }
    document.querySelectorAll('[data-brlm-page-head]').forEach(node => { if (node.tagName === 'SCRIPT') { const script = document.createElement('script'); for (const attr of node.attributes) if (attr.name !== 'data-brlm-page-head') script.setAttribute(attr.name, attr.value); script.textContent = node.textContent; script.setAttribute('data-brlm-page-head', ''); node.replaceWith(script); } });
    stage.innerHTML = packet.body || '';
    activateScripts(stage);
    document.dispatchEvent(new CustomEvent('beryllium:present', {detail: {version:packet.version}}));
  }
  function process(packet) {
    if (packet.type === 'identity') {
      window.beryllium.clientId = packet.id;
      window.beryllium.groupId = packet.groupId;
      renderState(packet);
    } else if (packet.type === 'state') renderState(packet);
    else if (packet.type === 'present') present(packet);
    else if (packet.type === 'reload') location.reload();
    else if (packet.type === 'notification') show(packet.message, packet.title, packet.level, packet.duration);
    else if (packet.type === 'update') {
      let nodes;
      try { nodes = document.querySelectorAll(packet.selector); } catch { return; }
      nodes.forEach(node => {
        if ('text' in packet) node.textContent = packet.text;
        if ('html' in packet) node.innerHTML = packet.html;
        if ('value' in packet && 'value' in node) node.value = packet.value;
        if (packet.styles) Object.entries(packet.styles).forEach(([k,v]) => node.style.setProperty(k.replaceAll('_','-'),v));
        if (packet.attributes) Object.entries(packet.attributes).forEach(([k,v]) => v === null ? node.removeAttribute(k) : node.setAttribute(k,v));
      });
    } else if (packet.type === 'event') {
      (listeners.get(packet.name) || []).forEach(fn => fn(packet.data));
      document.dispatchEvent(new CustomEvent('beryllium:' + packet.name, {detail: packet.data}));
    }
  }
  // sessionStorage is isolated per browser tab (unlike shared cookies).
  let tabId;
  try {
    tabId = sessionStorage.getItem('brlm-tab-id');
    if (!/^[a-f0-9]{32}$/.test(tabId || '')) {
      tabId = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
      sessionStorage.setItem('brlm-tab-id', tabId);
    }
  } catch (_) {
    tabId = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
  }
  function connect() {
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws?tab=' + encodeURIComponent(tabId));
    ws.addEventListener('open', () => { connected = true; retry = 500; while(queue.length) ws.send(queue.shift()); });
    ws.addEventListener('message', e => { try { process(JSON.parse(e.data)); } catch(err) { console.error(err); } });
    ws.addEventListener('error', e => console.warn('Beryllium WebSocket connection failed', e));
    ws.addEventListener('close', () => { connected = false; stopAll(); setTimeout(connect, retry); retry = Math.min(retry*2, 10000); });
  }
  window.beryllium = {
    clientId: null,
    groupId: null,
    get connected() { return connected; },
    on(name, cb) { if(!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(cb); },
    emit(name, data={}) {
      const packet = JSON.stringify({type:'event',name,data});
      if (connected && ws.readyState === WebSocket.OPEN) ws.send(packet); else if(queue.length < 100) queue.push(packet);
    },
    notify: show
  };
  window.berylliumAction = async (key, data={}) => {
    const r = await fetch('/api/' + encodeURIComponent(key), {method:'POST',headers:{'Content-Type':'application/json','X-Beryllium-Tab':tabId},body:JSON.stringify(data)});
    const result = await r.json();
    if (!r.ok) { show(result.detail || 'Action failed', 'Action unavailable', 'warning'); throw Error(result.detail || 'Action failed'); }
    return result;
  };
  window.berylliumStartHold = (el,key,ms=100) => {
    if (holds.has(el)) return;
    berylliumAction(key).catch(console.error);
    holds.set(el, setInterval(() => berylliumAction(key).catch(console.error), ms));
  };
  window.berylliumStopHold = stopHold;
  for (const event of ['pointerup','pointercancel','blur']) window.addEventListener(event,stopAll);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)stopAll();});
})();
'''

_CLIENT_CSS = '''
#brlm-notifications{position:fixed;right:16px;top:16px;z-index:99999;display:flex;flex-direction:column;gap:8px;max-width:min(90vw,360px);pointer-events:none}
.brlm-notification{background:#202027;color:white;padding:14px 18px;border-radius:10px;border-left:4px solid #89b4fa;box-shadow:0 6px 22px #0006;pointer-events:auto;font:14px system-ui}
#brlm-notifications .brlm-notification{position:relative;box-sizing:border-box;min-width:240px;max-width:100%;padding:14px 44px 14px 18px;text-align:left;line-height:1.4}
#brlm-notifications .brlm-notification strong{display:block;margin:0;font-size:14px;line-height:1.4}
#brlm-notifications .brlm-notification p{margin:4px 0 0;overflow-wrap:anywhere;font-size:14px;line-height:1.4}
#brlm-notifications .brlm-notification button.brlm-notification-close{all:unset;box-sizing:border-box;position:absolute;top:8px;right:8px;width:26px;height:26px;display:flex;align-items:center;justify-content:center;border-radius:6px;color:#ddd;background:transparent;font:20px/1 system-ui;cursor:pointer;z-index:1}
#brlm-notifications .brlm-notification button.brlm-notification-close:hover{background:#ffffff22;color:#fff}
#brlm-notifications .brlm-notification button.brlm-notification-close:focus-visible{outline:2px solid #89b4fa;outline-offset:2px}
.brlm-notification p{margin:6px 0 0}.brlm-warning{border-color:#f9c74f}.brlm-error{border-color:#ef476f}.brlm-success{border-color:#4ade80}
#brlm-state{position:fixed;inset:0;z-index:99998;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#111ef;color:white;font:18px system-ui;text-align:center;padding:20px}
#brlm-state h2{font-size:28px;margin:0 0 10px}#brlm-state p{max-width:520px}
.brlm-info{border-color:#89b4fa}
'''

_ADMIN_HTML = '''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Beryllium Admin</title>
<style>
body{font:14px system-ui;background:#111117;color:#eee;margin:25px}h1{color:#ff9a91}section{background:#1c1c24;padding:16px;margin:16px 0;border-radius:12px}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:9px;border-bottom:1px solid #333}button,input,textarea,select{background:#30303b;color:white;border:1px solid #555;padding:8px;border-radius:6px}button{cursor:pointer;margin:2px}button:hover{background:#444453}textarea{width:98%;height:95px;font:13px monospace}pre{white-space:pre-wrap;min-height:30px;color:#b7e2b0}small{color:#aaa}summary{cursor:pointer;font-weight:600}details>summary{padding:5px 0}.group-row{background:#292934}.session-row{background:#1a1a23}.muted{color:#aaa}.controls{min-width:240px}.scene-select{max-width:180px;margin:2px}.scene-meta{color:#aab0c2;font-size:12px}.scene-section[hidden]{display:none}
</style></head><body>
<h1>Beryllium Host Panel</h1><small>Local-only administration. The Python console executes code with full host-process privileges.</small>
<section><details id="devices" open><summary>Connected devices (<span id="device-count">0</span> sessions)</summary><table><thead><tr><th>Group / Session</th><th>Name</th><th>Device</th><th>IP</th><th>Status</th><th>Scene</th><th>Controls</th></tr></thead><tbody id="clients"></tbody></table></details></section>
<section id="scene-section" class="scene-section" hidden><details><summary>Scenes</summary><p class="scene-meta">Set the default page for clients without overrides. Client and group selectors appear under Connected devices.</p><label>Default scene <select id="global-scene" onchange="presentScene('global',this.value)"></select></label><pre id="sceneout"></pre></details></section>
<section><details><summary>Broadcast notification</summary><input id="title" placeholder="Title" value="Announcement"><input id="message" placeholder="Message"><button onclick="notifyAll()">Broadcast</button></details></section>
<section><details><summary>Command console</summary><small>help, groups, clients, pause 0, resume 0:1, kick 0, ban 0, notify 0:1 Message, broadcast Hello, present global Lobby, present 0:1 Game, present 0 inherit</small><textarea id="cmd" placeholder="help"></textarea><button onclick="runCommand()">Run</button><pre id="cmdout"></pre></details></section>
<section><details><summary>Python console (host PC)</summary><small>Full Python execution. Available: host, cgroups, Group, Client. Only use on a trusted local machine.</small><textarea id="py" spellcheck="false" placeholder="print('hi')
host.broadcast('Hello!')"></textarea><button onclick="runPython()">Execute Python</button><pre id="pyout"></pre></details></section>
<section><details><summary>JavaScript console</summary><textarea id="js" placeholder="document.title"></textarea><button onclick="runJS()">Run in admin browser</button><pre id="jsout"></pre></details></section>
<script>
async function api(path,data){const r=await fetch('/__brlm_admin/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const j=await r.json();if(!r.ok)throw Error(j.detail);return j;}
const expanded=new Set();let initialized=false;let sceneNames=[];let defaultScene=null;const pendingSelections=new Map();
function sceneSelector(target,scene,effective){
 const select=document.createElement('select');select.className='scene-select';select.dataset.target=target;
 const inherit=document.createElement('option');inherit.value='';inherit.textContent='Inherit ('+(effective||'unlisted')+')';select.append(inherit);
 for(const name of sceneNames){const option=document.createElement('option');option.value=name;option.textContent=name;select.append(option);}
 select.value=pendingSelections.has(target)?pendingSelections.get(target):(scene||'');select.onchange=()=>{pendingSelections.set(target,select.value);presentScene(target,select.value);};
 return select;
}
async function presentScene(target,scene){try{await api('present',{target,scene});pendingSelections.delete(target);await refresh();if(target==='global')document.getElementById('sceneout').textContent='Default scene updated';}catch(e){const out=document.getElementById('sceneout');out.textContent=e.message;alert(e.message);}}
async function loadScenes(){const r=await fetch('/__brlm_admin/scenes');if(!r.ok)throw Error('Cannot load scenes');const j=await r.json();sceneNames=j.scenes;defaultScene=j.default;const section=document.getElementById('scene-section');section.hidden=!sceneNames.length;const global=document.getElementById('global-scene');global.replaceChildren();for(const name of sceneNames){const opt=document.createElement('option');opt.value=name;opt.textContent=name;global.append(opt);}global.value=defaultScene||'';if(!global.value&&sceneNames.length){const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='Select a scene…';global.prepend(placeholder);global.value='';}}
function cell(tr,value){const td=document.createElement('td');td.textContent=String(value??'');tr.append(td);return td;}
function control(td,cmd,target){const b=document.createElement('button');b.textContent=cmd;b.onclick=async()=>{try{if(cmd==='notify'){const message=prompt('Notification for '+target);if(message===null||!message.trim())return;await api('notify',{target,message,title:'Notice'});}else{let reason='';if(cmd==='resume'){if(!confirm('Resume '+target+'?'))return;}else{const input=prompt('Reason for '+cmd,'');if(input===null)return;reason=input;}await api('manage',{id:target,command:cmd,reason});}await refresh();}catch(e){alert(e.message)}};td.append(b);}
async function refresh(){try{await loadScenes();const r=await fetch('/__brlm_admin/clients');const j=await r.json();if(!r.ok)throw Error(j.detail);const root=document.getElementById('clients');root.replaceChildren();const groups=j.clients.filter(c=>c.kind==='group');const clients=j.clients.filter(c=>c.kind==='client');document.getElementById('device-count').textContent=clients.length;
for(const g of groups){const tr=document.createElement('tr');tr.className='group-row';const td=cell(tr,'');const toggle=document.createElement('button');const isOpen=expanded.has(g.target);toggle.textContent=(isOpen?'▾ ':'▸ ')+g.name+' ('+clients.filter(c=>c.target.startsWith(g.target+':')).length+')';toggle.onclick=()=>{if(expanded.has(g.target))expanded.delete(g.target);else expanded.add(g.target);refresh()};td.append(toggle);cell(tr,g.name);cell(tr,'—');cell(tr,g.ip||'');cell(tr,g.status);cell(tr,g.effective_scene||'Unlisted');const controls=cell(tr,'');controls.className='controls';for(const cmd of ['notify','pause','resume','kick','ban'])control(controls,cmd,g.target);if(sceneNames.length)controls.append(sceneSelector(g.target,g.scene,g.effective_scene));root.append(tr);
if(isOpen)for(const c of clients.filter(c=>c.target.startsWith(g.target+':'))){const row=document.createElement('tr');row.className='session-row';cell(row,'↳ '+c.target);cell(row,c.name);cell(row,c.device_type||'Unknown');cell(row,'');cell(row,c.status);cell(row,c.effective_scene||'Unlisted');const actions=cell(row,'');actions.className='controls';for(const cmd of ['notify','pause','resume','kick'])control(actions,cmd,c.target);if(sceneNames.length)actions.append(sceneSelector(c.target,c.scene,c.effective_scene));root.append(row);}}
if(!initialized){for(const g of groups)expanded.add(g.target);initialized=true;if(groups.length)refresh();}
}catch(e){document.getElementById('cmdout').textContent=e.message}}
async function notifyAll(){try{const message=document.getElementById('message').value;if(!message.trim())return;await api('notify',{title:document.getElementById('title').value,message});}catch(e){alert(e.message)}}
async function runCommand(){try{const j=await api('console',{command:document.getElementById('cmd').value});document.getElementById('cmdout').textContent=j.output;await refresh()}catch(e){document.getElementById('cmdout').textContent=e.message}}
async function runPython(){try{const j=await api('python',{code:document.getElementById('py').value});document.getElementById('pyout').textContent=j.output||'(no output)';await refresh()}catch(e){document.getElementById('pyout').textContent=e.message}}
function runJS(){try{const code=document.getElementById('js').value;const result=Function('"use strict";return ('+code+')')();document.getElementById('jsout').textContent=String(result)}catch(e){document.getElementById('jsout').textContent=String(e)}}
refresh();setInterval(refresh,2000);
</script></body></html>''' 
class Host:
    def __init__(self, port=8000, host='0.0.0.0', max_clients=None, bans_file='beryllium_bans.json', admin_panel=True):
        self.host, self.port = host, port
        self.max_clients = max_clients
        self.page = Page('index')
        self.default_method = ON_RELEASE
        self.actions: dict[str, Callable] = {}
        self.events: dict[str, Callable] = {}
        self.cgroups = ClientGroups(self)
        self.clients = self.cgroups  # Deprecated alias
        self._bans_file = Path(bans_file) if bans_file else None
        self._bans = {}
        if self._bans_file and self._bans_file.exists():
            self._bans = json.loads(self._bans_file.read_text())
        self._admin_panel = admin_panel
        self._admin_key = secrets.token_urlsafe(32)
        self._python_scope = {"__builtins__": __builtins__, "host": self, "cgroups": self.cgroups, "Group": Group, "Client": Client}
        self._exposed_pages = {}  # admin scene name -> Fluorine Page
        self._loop = None
        self._thread_id = None
        self._html = None
        self._built = False
        self._join_callbacks = []
        self._connect_callbacks = []
        self._disconnect_callbacks = []
        self._notification_css = _CLIENT_CSS
        self._page_actions = {}  # action key -> (page, callback)
        self._page_action_ids = {}  # (page identity, callback identity) -> key
        self._page_versions = {}  # page identity -> incrementing render version
        self._rendered_pages = {}  # page identity -> (version, head, body)
        self._render_lock = threading.RLock()
        @asynccontextmanager
        async def lifespan(app):
            self._loop = asyncio.get_running_loop()
            self._thread_id = threading.get_ident()
            try:
                yield
            finally:
                self._loop = None
                self._thread_id = None
        self.app = FastAPI(lifespan=lifespan)
        self._routes()

    def _save_bans(self):
        if self._bans_file:
            self._bans_file.parent.mkdir(parents=True, exist_ok=True)
            self._bans_file.write_text(json.dumps(self._bans, indent=2))

    def _admin_allowed(self, request):
        return self._admin_panel and _loopback(request) and request.cookies.get('brlm_host') == self.cgroups.get_host()._token

    def _resolve_request(self, request, create=True):
        ip = request.client.host if request.client else None
        if ip in self._bans:
            raise HTTPException(403, 'This IP has been banned: ' + self._bans[ip])
        token = request.cookies.get('brlm_session')
        host_token = request.cookies.get('brlm_host') if _loopback(request) else None
        if host_token == self.cgroups.get_host()._token and not request.headers.get('x-beryllium-tab'):
            return self.cgroups.get_host(), False
        tab = request.headers.get('x-beryllium-tab')
        client = (self.cgroups._resolve_tab(token, ip, tab, create=False) if tab
                  else self.cgroups._resolve(token, ip, create=create))
        if client is None:
            raise HTTPException(403, 'Host is full or this session is unavailable')
        if client.id != "HOST":
            self._identify_device(client, request.headers.get("user-agent", ""))
        return client, token != client._token

    def _identify_device(self, client, agent):
        if not agent: return
        client.user_agent = agent[:512]
        a = agent.lower()
        if any(x in a for x in ('iphone', 'ipad', 'ipod')): kind = 'Phone' if 'ipad' not in a else 'Tablet'
        elif 'android' in a: kind = 'Tablet' if 'mobile' not in a else 'Phone'
        elif 'mobile' in a: kind = 'Phone'
        elif any(x in a for x in ('windows', 'macintosh', 'x11', 'linux')): kind = 'Desktop'
        else: kind = 'Unknown'
        client.device_type = kind

    def _routes(self):
        @self.app.get('/')
        async def index(request: Request):
            client, new = self._resolve_request(request)
            if self._effective_state(client)[0] in ('kicked','banned'):
                return HTMLResponse('<h1>Session unavailable</h1><p>'+escape(self._effective_state(client)[1])+'</p>', status_code=403)
            if self._html is None:
                raise HTTPException(503, 'Server starting')
            response = FileResponse(self._html)
            if new:
                response.set_cookie('brlm_session', client._token, httponly=True, samesite='strict')
                await self._callbacks(self._join_callbacks, client)
            return response

        @self.app.post('/api/{key}')
        async def dispatch(key: str, request: Request):
            client, _ = self._resolve_request(request, create=False)
            self._check_active(client)
            callback = self.actions.get(key)
            registered_page = self._page_actions.get(key)
            if registered_page is not None:
                if registered_page[0] is not self._effective_page(client):
                    raise HTTPException(403, 'Action is not available on this page')
                callback = registered_page[1]
            if callback is None:
                raise HTTPException(404, 'Unknown action')
            try:
                payload = await request.json()
            except ValueError:
                raise HTTPException(400, 'Invalid JSON')
            if not isinstance(payload, dict):
                raise HTTPException(400, 'Expected JSON object')
            try:
                signature = inspect.signature(callback)
                if 'client' in signature.parameters:
                    if 'client' in payload:
                        raise HTTPException(400, 'Client cannot be supplied')
                    payload['client'] = client
                signature.bind(**payload)
            except TypeError as e:
                raise HTTPException(400, 'Invalid action arguments') from e
            result = callback(**payload)
            if inspect.isawaitable(result):
                result = await result
            return {'result': result}

        @self.app.websocket('/ws')
        async def websocket_endpoint(ws: WebSocket):
            ip = ws.client.host if ws.client else None
            if ip in self._bans:
                await ws.close(code=1008)
                return
            token = ws.cookies.get('brlm_session')
            host_token = ws.cookies.get('brlm_host') if _loopback(ws) else None
            client = (self.cgroups._resolve_tab(token, ip, ws.query_params.get('tab'), create=True)
                      if token else self.cgroups.get_host() if host_token == self.cgroups.get_host()._token else None)
            if client is None:
                print(f'BRLM WebSocket rejected: unknown session from {ip}; reload page to obtain a session cookie')
                await ws.close(code=1008)
                return
            if self._effective_state(client)[0] in ('kicked','banned'):
                await ws.close(code=1008)
                return
            origin = ws.headers.get('origin')
            expected = ws.headers.get('host')
            if origin and urlsplit(origin).netloc != expected:
                print(f'BRLM WebSocket rejected: origin {origin!r} does not match host {expected!r}')
                await ws.close(code=1008)
                return
            await ws.accept()
            if client.id != "HOST": self._identify_device(client, ws.headers.get("user-agent", ""))
            first = not client._sockets
            client._sockets.add(ws)
            client._online = True
            state, reason = self._effective_state(client)
            await ws.send_json({'type':'identity','id':client.id,'groupId':client.group.id,'status':state,'reason':reason})
            await ws.send_json(self._page_packet(self._effective_page(client)))
            if first:
                await self._callbacks(self._connect_callbacks, client)
            self._refresh_admin()
            try:
                while True:
                    packet = await ws.receive_json()
                    if ip in self._bans or self._effective_state(client)[0] != 'active' or not isinstance(packet, dict) or packet.get('type') != 'event':
                        continue
                    name, data = packet.get('name'), packet.get('data', {})
                    callback = self.events.get(name)
                    if callback is None or not isinstance(data, dict):
                        continue
                    try:
                        if 'client' in inspect.signature(callback).parameters:
                            if 'client' in data: continue
                            data['client'] = client
                        else:
                            data = {'client_id':client.id, **data}
                        result = callback(**data)
                        if inspect.isawaitable(result):
                            await result
                    except Exception as e:
                        print(f'Beryllium event {name!r} failed: {e}')
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                client._sockets.discard(ws)
                if not client._sockets:
                    client._online = False
                    await self._callbacks(self._disconnect_callbacks, client)
                self._refresh_admin()

        @self.app.get('/__brlm_admin/claim')
        async def claim(request: Request, key: str):
            if not self._admin_panel or not _loopback(request) or not secrets.compare_digest(key, self._admin_key):
                raise HTTPException(403, 'Host panel is local-only')
            response = HTMLResponse('<meta http-equiv="refresh" content="0;url=/__brlm_admin">')
            response.set_cookie('brlm_host', self.cgroups.get_host()._token, httponly=True, samesite='strict')
            return response

        @self.app.get('/__brlm_admin')
        async def admin(request: Request):
            if not self._admin_allowed(request): raise HTTPException(403, 'Not authorized')
            return HTMLResponse(_ADMIN_HTML)

        @self.app.get('/__brlm_admin/clients')
        async def admin_clients(request: Request):
            if not self._admin_allowed(request): raise HTTPException(403)
            return {'clients':[entry for group in self.cgroups for entry in [self._client_info(group), *(self._client_info(client) for client in group)]]}

        @self.app.get('/__brlm_admin/scenes')
        async def admin_scenes(request: Request):
            if not self._admin_allowed(request): raise HTTPException(403, 'Not authorized')
            return {'scenes':list(self._exposed_pages), 'default':self._scene_name(self.page)}

        @self.app.post('/__brlm_admin/present')
        async def admin_present(request: Request):
            if not self._admin_allowed(request): raise HTTPException(403, 'Not authorized')
            data = await request.json()
            if not isinstance(data, dict): raise HTTPException(400, 'Invalid request')
            target, scene = data.get('target'), data.get('scene')
            if not isinstance(scene, str): raise HTTPException(400, 'Invalid scene')
            if scene != '' and scene not in self._exposed_pages:
                raise HTTPException(404, 'Unknown scene')
            page = self._exposed_pages.get(scene) if scene else None
            if target == 'global':
                if page is None: raise HTTPException(400, 'Choose a scene for the default page')
                self.page = page
                self._present_default_changed()
            else:
                obj = self._target(target)
                obj.present(page)
            return {'ok':True}

        @self.app.post('/__brlm_admin/manage')
        async def manage(request: Request):
            if not self._admin_allowed(request): raise HTTPException(403)
            data = await request.json()
            return {'output':self._manage(data.get('command'), data.get('id'), data.get('reason',''))}

        @self.app.post('/__brlm_admin/notify')
        async def admin_notify(request: Request):
            if not self._admin_allowed(request): raise HTTPException(403)
            data = await request.json()
            target = data.get('target')
            recipient = self._target(target) if target is not None else None
            await self._send_packet({'type':'notification','message':str(data.get('message','')),'title':str(data.get('title','Announcement')),'level':'info','duration':10000}, recipient)
            return {'ok':True}

        @self.app.post('/__brlm_admin/python')
        async def python_console(request: Request):
            if not self._admin_allowed(request): raise HTTPException(403)
            data = await request.json()
            code = data.get('code', '')
            if not isinstance(code, str) or len(code) > 20000: raise HTTPException(400, 'Invalid Python code')
            output = io.StringIO()
            def console_print(*args, sep=' ', end='\n', file=None, flush=False):
                print(*args, sep=sep, end=end, file=output if file is None else file, flush=flush)
            scope = self._python_scope
            scope['print'] = console_print
            try:
                compiled = compile(code, '<beryllium-admin>', 'exec')
                exec(compiled, scope, scope)
            except BaseException:
                traceback.print_exc(file=output)
            return {'output':output.getvalue()}

        @self.app.post('/__brlm_admin/console')
        async def console(request: Request):
            if not self._admin_allowed(request): raise HTTPException(403)
            data = await request.json()
            command = str(data.get('command','')).strip()
            parts = command.split(maxsplit=2)
            if not parts: return {'output':''}
            return {'output':self._command_console(command)}

    def _target(self, target):
        if not isinstance(target, str): raise HTTPException(400, 'Invalid target')
        try:
            ids = [int(x) for x in target.split(':')]
            if len(ids) not in (1, 2) or any(x < 0 for x in ids): raise ValueError()
            obj = self.cgroups[ids[0]]
            return obj if len(ids) == 1 else obj[ids[1]]
        except (ValueError, IndexError):
            raise HTTPException(404, 'Unknown group/client')

    def _command_console(self, command):
        parts = command.strip().split(maxsplit=2)
        if not parts: return ''
        op = parts[0].lower()
        if op == 'help':
            return 'groups | clients G | pause G[:C] [reason] | resume G[:C] | kick G[:C] [reason] | ban G [reason] | rename G[:C] NAME | unban IP | broadcast MESSAGE | notify G[:C] MESSAGE | present global|G[:C] SCENE|inherit'
        if op == 'groups':
            return '\n'.join(f'{g.id}: {g.name} {g.ip} ({g.status}, {len(g)} sessions)' for g in self.cgroups) or 'No groups'
        if op == 'clients':
            if len(parts) == 1:
                return '\n'.join(f'{c.full_id} {c.name} ({self._effective_state(c)[0]}, {"online" if c.online() else "offline"})' for g in self.cgroups for c in g) or 'No clients'
            g = self.cgroups[int(parts[1])]
            return '\n'.join(f'{c.full_id} {c.name} ({self._effective_state(c)[0]})' for c in g) or 'No clients'
        if op == 'unban':
            if len(parts)<2: raise HTTPException(400,'Usage: unban IP')
            self.cgroups.unban(parts[1]); return 'Unbanned '+parts[1]
        if op == 'broadcast':
            if len(parts)<2: raise HTTPException(400,'Usage: broadcast MESSAGE')
            self.notify(command.split(maxsplit=1)[1], title='Broadcast')
            return 'Broadcast queued'
        if op == 'present':
            if len(parts) < 3:
                raise HTTPException(400, 'Usage: present global|G[:C] SCENE|inherit')
            target, name = parts[1], parts[2].strip()
            if target == 'global' and name.lower() == 'inherit':
                raise HTTPException(400, 'Global presentation requires an exposed scene')
            if name.lower() == 'inherit':
                page = None
            else:
                page = self._exposed_pages.get(name)
                if page is None:
                    raise HTTPException(404, f'Unknown scene: {name}. Available: {", ".join(self._exposed_pages) or "none"}')
            if target == 'global':
                self.page = page
                self._present_default_changed()
            else:
                self._target(target).present(page)
            return f'Presented {name} to {target}'
        if op in ('pause','resume','kick','ban','rename','notify'):
            if len(parts)<2: raise HTTPException(400,f'Usage: {op} G[:C] [message]')
            target = parts[1]
            try:
                ids = [int(i) for i in target.split(':')]
                if len(ids) not in (1,2) or any(i<0 for i in ids): raise ValueError()
                obj = self.cgroups[ids[0]]
                if len(ids)==2: obj = obj[ids[1]]
            except (ValueError, IndexError): raise HTTPException(404,'Unknown group/client')
            message = parts[2] if len(parts)>2 else ''
            if op == 'ban' and not isinstance(obj, Group): raise HTTPException(400,'Only groups can be banned')
            if op == 'notify':
                if not message: raise HTTPException(400,'Usage: notify G[:C] MESSAGE')
                obj.notify(message); return f'Notification queued for {target}'
            try:
                if op == 'resume': obj.resume()
                elif op == 'rename': obj.rename(message)
                else: getattr(obj,op)(message or {'pause':'Paused by host','kick':'Kicked by host','ban':'Banned by host'}[op])
            except ValueError as e: raise HTTPException(400,str(e)) from e
            return f'{op}: {target}'
        raise HTTPException(400,'Unknown command. Type help.')

    def _scene_name(self, page):
        for name, exposed in self._exposed_pages.items():
            if exposed is page: return name
        return None

    def _present_default_changed(self):
        if self._loop is None: return
        for group in self.cgroups:
            for client in group:
                if client._presented_page is None and group._presented_page is None:
                    self._present_changed(client)

    def _client_info(self, obj):
        if isinstance(obj, Group):
            return {'id':str(obj.id),'name':obj.name,'ip':obj.ip,'online':obj.online(),
                    'status':obj.status,'kind':'group','target':str(obj.id),
                    'scene':self._scene_name(obj._presented_page),
                    'effective_scene':self._scene_name(obj._presented_page or self.page)}
        state, _ = self._effective_state(obj)
        return {'id':obj.id,'name':obj.name,'online':obj.online(), 'device_type':obj.device_type,
                'status':state,'kind':'host' if obj.id=='HOST' else 'client',
                'target':'HOST' if obj.id=='HOST' else f'{obj.group.id}:{obj.id}',
                 'scene':self._scene_name(obj._presented_page),
                 'effective_scene':self._scene_name(self._effective_page(obj))}

    def _manage(self, command, client_id, reason):
        return self._command_console(f'{command} {client_id}' + (f' {reason}' if reason else ''))

    def _effective_state(self, client):
        if client.group.status != 'active':
            return client.group.status, client.group.reason
        return client.status, client.reason

    def _group_state_changed(self, group):
        for client in group: self._state_changed(client)
        self._refresh_admin()

    def _check_active(self, client):
        state, reason = self._effective_state(client)
        if state != 'active':
            raise HTTPException(403, f'Session {state}: {reason}')

    async def _callbacks(self, callbacks, client):
        for fn in callbacks:
            try:
                result = fn(client)
                if inspect.isawaitable(result): await result
            except Exception as e:
                print(f'Beryllium callback failed: {e}')

    def on_join(self, fn):
        self._join_callbacks.append(fn); return fn

    def on_connect(self, fn):
        self._connect_callbacks.append(fn); return fn

    def on_disconnect(self, fn):
        self._disconnect_callbacks.append(fn); return fn

    def action(self, key):
        self._validate_key(key)
        def decorator(fn):
            if key in self.actions: raise ValueError('Action already registered: '+key)
            self.actions[key] = fn
            return fn
        return decorator

    def on(self, key):
        self._validate_key(key)
        def decorator(fn):
            if key in self.events: raise ValueError('Event already registered: '+key)
            self.events[key] = fn
            return fn
        return decorator

    @staticmethod
    def _validate_key(key):
        if not isinstance(key,str) or not _KEY.fullmatch(key):
            raise ValueError('Keys must start with a letter and contain letters, digits, _ or -')

    @staticmethod
    def _validate_page(page):
        if page is None:
            return
        if not all(callable(getattr(page, name, None)) for name in (
            "build", "register_python_handler"
        )):
            raise TypeError("present() expects a Fluorine-compatible Page or None")

    def _effective_page(self, client):
        return client._presented_page or client.group._presented_page or self.page

    def _page_packet(self, page):
        self._validate_page(page)
        with self._render_lock:
            identity = id(page)
            # Render on demand so changes to a Page are reflected on next present/refresh.
            version = self._page_versions.get(identity, 0)
            cached = self._rendered_pages.get(identity)
            if cached is None or cached[0] != version:
                def register(handler):
                    key = (identity, id(handler))
                    action = self._page_action_ids.get(key)
                    if action is None:
                        action = 'fluorine_' + secrets.token_hex(12)
                        self._page_action_ids[key] = action
                    self._page_actions[action] = (page, handler)
                    return 'berylliumAction(' + json.dumps(action) + ')'
                # Fluorine Page.build uses register_python_handler while
                # converting callable HTML attributes into browser events.
                original = page.register_python_handler
                page.register_python_handler = register
                try:
                    with TemporaryDirectory(prefix='brlm-page-') as directory:
                        path = Path(directory) / 'page.html'
                        page.build(str(path))
                        head, body = _page_parts(path.read_text(encoding='utf-8'))
                finally:
                    page.register_python_handler = original
                cached = (version, head, body)
                self._rendered_pages[identity] = cached
            return {'type':'present','version':f'{identity}:{cached[0]}',
                    'head':cached[1], 'body':cached[2]}

    def _present_changed(self, target):
        if self._loop is None:
            return
        clients = list(target) if isinstance(target, Group) else [target]
        for client in clients:
            self._submit(self._send_packet(self._page_packet(self._effective_page(client)), client))

    def refresh_page(self, page=None):
        """Re-render a Fluorine page and update only its current audience."""
        page = self.page if page is None else page
        self._validate_page(page)
        with self._render_lock:
            identity = id(page)
            self._page_versions[identity] = self._page_versions.get(identity, 0) + 1
            self._rendered_pages.pop(identity, None)
        if self._loop is not None:
            for group in self.cgroups:
                for client in group:
                    if self._effective_page(client) is page:
                        self._present_changed(client)
        return self

    def _refresh_admin(self):
        pass  # The admin panel polls /__brlm_admin/clients.

    async def _send_packet(self, packet, client=None):
        targets = ([client] if isinstance(client, Client) else list(client) if isinstance(client, Group)
                   else [c for g in self.cgroups for c in g])
        for c in targets:
            for ws in tuple(c._sockets):
                try: await ws.send_json(packet)
                except Exception as e:
                    c._sockets.discard(ws)
                    print(f'BRLM: failed to send {packet.get("type")} to {c.full_id}: {e}')

    def _submit(self, coroutine):
        if self._loop is None:
            coroutine.close()
            raise RuntimeError('Host is not running')
        if threading.get_ident() == self._thread_id:
            return self._loop.create_task(coroutine)
        return asyncio.run_coroutine_threadsafe(coroutine,self._loop)

    def _state_changed(self, client):
        if self._loop is None: return
        status, reason = self._effective_state(client)
        self._submit(self._send_packet({'type':'state','status':status,'reason':reason},client))

    def broadcast(self, name, data=None):
        # A plain string broadcasts a visible notification. Name + data emits a JS event.
        if data is None:
            return self.notify(name, title='Broadcast')
        self._validate_key(name)
        return self._submit(self._send_packet({'type':'event','name':name,'data':data}))

    def broadcast_event(self, name, data=None):
        self._validate_key(name)
        return self._submit(self._send_packet({'type':'event','name':name,'data':data}))

    def send(self, client_id, name, data=None):
        self._validate_key(name)
        client = (client_id if isinstance(client_id, Client) else self.cgroups.get_host() if client_id == 'HOST'
                  else self.cgroups[client_id[0]][client_id[1]] if isinstance(client_id, tuple) else None)
        if client is None: raise ValueError('Use a Client, HOST, or (group_id, client_id)')
        return self._submit(self._send_packet({'type':'event','name':name,'data':data},client))

    def update(self, selector, *, text=None, html=None, value=None, styles=None, attributes=None, client=None):
        if text is not None and html is not None: raise ValueError('Use either text or html')
        packet = {'type':'update','selector':selector}
        for k,v in {'text':text,'html':html,'value':value,'styles':styles,'attributes':attributes}.items():
            if v is not None: packet[k] = v
        return self._submit(self._send_packet(packet,client))

    def notify(self, message, title='Notice', level='info', duration=10000, client=None):
        if level not in ('info','warning','error','success'): raise ValueError('Invalid notification level')
        packet = {'type':'notification','message':str(message),'title':str(title),'level':level,'duration':int(duration)}
        return self._submit(self._send_packet(packet,client))

    def announce(self, message, title='Announcement', **kwargs):
        return self.notify(message,title=title,**kwargs)

    def notification_style(self, css):
        if self._built: raise RuntimeError('Configure notification styling before run()')
        self._notification_css += '\n'+str(css)
        return self

    def run(self, open=False, debug=False, static_dir=None, admin=None, sulfur=True):
        if self._built: raise RuntimeError('Host instance already run')
        self._built = True
        if static_dir: self.app.mount('/static',StaticFiles(directory=static_dir),name='static')
        # Beryllium's transport lives in a persistent shell, independently of
        # whichever Fluorine page is presented to each browser session.
        shell = Page('beryllium')
        shell.script(Script(_CLIENT_JS))
        shell.style('html', margin='0', padding='0')
        shell.head(Element('style', RawHTML(self._notification_css)))
        with TemporaryDirectory(prefix='beryllium-') as directory:
            self._html = Path(directory)/'index.html'
            shell.build(str(self._html))
            address = _lan_ip() if self.host == '0.0.0.0' else self.host
            if debug: print(f'{THEME_COLOR}BERYL{Color.reset}:    Hosting at http://{address}:{self.port}')
            admin_url = f'http://127.0.0.1:{self.port}/__brlm_admin/claim?key={self._admin_key}'
            if admin is None: admin = self._admin_panel
            def launch_when_ready(fn):
                import time
                for _ in range(100):
                    try:
                        with socket.create_connection(('127.0.0.1', self.port), timeout=0.2):
                            fn(); return
                    except OSError:
                        time.sleep(0.1)
            if admin and self._admin_panel and sulfur:
                try:
                    App = nitrogen.require('s').App
                except Exception as e:
                    print(f'{THEME_COLOR}BRLM{Color.reset}:     Sulfur unavailable; using browser admin panel: {e}')
                    sulfur = False
                else:
                    config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level='debug' if debug else 'critical', access_log=debug)
                    server = uvicorn.Server(config)
                    thread = threading.Thread(target=server.run, daemon=True)
                    thread.start()
                    try:
                        for _ in range(100):
                            if server.started: break
                            if not thread.is_alive(): raise RuntimeError('Server failed to start')
                            import time
                            time.sleep(0.1)
                        if not server.started: raise RuntimeError('Server startup timed out')
                        if open:
                            webbrowser.open(f'http://{address}:{self.port}')
                        App(admin_url, silent=not debug).open()
                    finally:
                        server.should_exit = True
                        thread.join(timeout=5)
                    self._html = None
                    return
            if admin and self._admin_panel:
                threading.Thread(target=lambda: launch_when_ready(lambda: webbrowser.open(admin_url)), daemon=True).start()
            if open:
                threading.Thread(target=lambda: launch_when_ready(lambda: webbrowser.open(f'http://{address}:{self.port}')), daemon=True).start()
            try:
                uvicorn.run(self.app,host=self.host,port=self.port,log_level='debug' if debug else 'critical',access_log=debug)
            finally:
                self._html = None
                if debug: print(f'{THEME_COLOR}BERYL{Color.reset}:    Host exit')

    def apply_styling(self):
        return apply_styling(self.page)
    
    def expose(self, name_or_func: str | callable, func: callable | None = None) -> Host:
        """Expose Python console objects; Fluorine pages also become admin scenes.

        host.expose(page) registers a scene under page.name (or its title).
        host.expose('lobby', page) registers an explicitly named scene.
        Ordinary functions/objects retain the previous console exposure behavior.
        """
        if isinstance(name_or_func, str):
            name, obj = name_or_func, func
        else:
            obj = name_or_func
            name = getattr(obj, '__name__', None) or getattr(obj, 'name', None) or getattr(obj, 'title', None)
            if not isinstance(name, str) or not name.strip():
                name = type(obj).__name__
        if obj is None: raise ValueError('expose() requires an object')
        self._python_scope[name] = obj
        if all(callable(getattr(obj, method, None)) for method in ('build', 'register_python_handler')):
            self._validate_page(obj)
            self._exposed_pages[name] = obj
        return func if isinstance(name_or_func, str) else name_or_func