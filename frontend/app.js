/* ═══════════════════════════════════════════════
   Taproot Wallet — Frontend SPA
   ═══════════════════════════════════════════════ */

const API = '';
let state = {
  wallets: [],
  balances: {},
  txs: {},
  musig2Sessions: [],
  activeMusig2Session: null,
  autoRefresh: true,
};

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  // Tab navigation
  document.querySelectorAll('.nav-item').forEach(el => {
    el.addEventListener('click', () => {
      showTab(el.dataset.tab);
    });
  });

  // Amount → BTC conversion
  document.getElementById('sendAmount').addEventListener('input', e => {
    const sat = parseInt(e.target.value) || 0;
    document.getElementById('sendAmountBtc').textContent =
      sat ? `≈ ${(sat / 1e8).toFixed(8)} BTC` : '';
  });

  loadAll();
  setInterval(() => { if (state.autoRefresh) refreshAll(); }, 30000);
});

// ── Navigation ────────────────────────────────────────────────────────────────

function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(t => t.classList.remove('active'));
  document.getElementById(`tab-${name}`).classList.add('active');
  document.querySelector(`[data-tab="${name}"]`).classList.add('active');

  if (name === 'dashboard') refreshDashboard();
  if (name === 'transactions') loadTxHistory();
  if (name === 'musig2') loadMusig2();
  if (name === 'receive' || name === 'send') populateWalletSelects();
}

// ── Log Panel ─────────────────────────────────────────────────────────────────

function uiLog(msg, level = 'INFO') {
  const el = document.getElementById('logEntries');
  if (!el) return;
  const colors = { INFO: '#8b949e', OK: '#3fb950', ERR: '#f85149', WARN: '#d29922' };
  const time = new Date().toTimeString().slice(0, 8);
  const line = document.createElement('div');
  line.innerHTML = `<span style="color:#444">${time}</span> <span style="color:${colors[level] || colors.INFO}">${level}:</span> ${msg}`;
  el.appendChild(line);
  el.parentElement.scrollTop = el.parentElement.scrollHeight;
  console.log(`[${level}] ${msg}`);
}

// ── API helpers ───────────────────────────────────────────────────────────────

async function api(path, opts = {}) {
  const method = opts.method || 'GET';
  uiLog(`${method} ${path}`);
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.detail || `HTTP ${res.status}`;
    uiLog(`${method} ${path} → ${res.status} ${msg}`, 'ERR');
    throw new Error(msg);
  }
  uiLog(`${method} ${path} → ${res.status} OK`, 'OK');
  return data;
}

const get  = path => api(path);
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });
const del  = path => api(path, { method: 'DELETE' });

// ── Load All ──────────────────────────────────────────────────────────────────

async function loadAll() {
  await loadWallets();
  refreshDashboard();
  loadMusig2();
}

async function loadWallets() {
  state.wallets = await get('/api/wallet/list').catch(() => []);
  renderWalletsTable();
  populateWalletSelects();
}

function refreshAll() {
  loadWallets().then(() => refreshDashboard());
  document.getElementById('lastRefresh').textContent =
    new Date().toLocaleTimeString('tr-TR');
}

// ── Dashboard ─────────────────────────────────────────────────────────────────

async function refreshDashboard() {
  document.getElementById('walletCount').textContent = state.wallets.length;

  const balFetches = state.wallets.map(async w => {
    try {
      const b = await get(`/api/wallet/${w.address}/balance`);
      state.balances[w.address] = b;
      return b;
    } catch { return null; }
  });

  const results = await Promise.all(balFetches);
  const valid = results.filter(Boolean);

  const totalSat   = valid.reduce((s, b) => s + b.total_sat, 0);
  const confirmedSat  = valid.reduce((s, b) => s + b.confirmed_sat, 0);
  const unconfirmedSat = valid.reduce((s, b) => s + b.unconfirmed_sat, 0);

  document.getElementById('totalBalance').textContent = `${totalSat.toLocaleString()} sat`;
  document.getElementById('totalBalanceBtc').textContent = `${(totalSat / 1e8).toFixed(8)} BTC`;
  document.getElementById('confirmedBalance').textContent = `${confirmedSat.toLocaleString()} sat`;
  document.getElementById('unconfirmedBalance').textContent = `${unconfirmedSat.toLocaleString()} sat`;

  // Wallet table
  const tbody = document.getElementById('dashboardWalletTable');
  if (!state.wallets.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">Cüzdan yok — Cüzdanlar sekmesinden ekleyin</td></tr>';
  } else {
    tbody.innerHTML = state.wallets.map(w => {
      const b = state.balances[w.address];
      const satDisplay = b ? `${b.total_sat.toLocaleString()} sat` : '…';
      const utxoDisplay = b ? b.utxo_count : '—';
      return `<tr>
        <td><span class="bold">${w.label}</span></td>
        <td><span class="mono truncate">${w.address}</span></td>
        <td><span class="orange bold">${satDisplay}</span></td>
        <td>${utxoDisplay}</td>
        <td>
          <button class="btn btn-ghost sm" onclick="quickReceive('${w.address}')">Al</button>
          <button class="btn btn-ghost sm" onclick="quickSend('${w.address}')">Gönder</button>
        </td>
      </tr>`;
    }).join('');
  }

  // Recent txs
  await loadRecentTxs();
}

async function loadRecentTxs() {
  const tbody = document.getElementById('recentTxTable');
  let allTxs = [];

  for (const w of state.wallets.slice(0, 3)) {
    try {
      const txs = await get(`/api/wallet/${w.address}/txs`);
      allTxs = allTxs.concat(txs.slice(0, 3).map(t => ({ ...t, _wallet: w.label })));
    } catch {}
  }

  allTxs.sort((a, b) => (b.status?.block_time || 0) - (a.status?.block_time || 0));

  if (!allTxs.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty">İşlem yok</td></tr>';
    return;
  }

  tbody.innerHTML = allTxs.slice(0, 8).map(tx => `
    <tr>
      <td><span class="mono-sm truncate">${tx.txid}</span></td>
      <td>${tx.status?.block_height ? `#${tx.status.block_height}` : '—'}</td>
      <td>${statusBadge(tx.status?.confirmed)}</td>
      <td>
        <a class="link mono-sm" href="https://mempool.space/testnet4/tx/${tx.txid}" target="_blank">↗</a>
      </td>
    </tr>
  `).join('');
}

// ── Receive ───────────────────────────────────────────────────────────────────

function updateReceive() {
  const address = document.getElementById('receiveWalletSelect').value;
  const addrEl = document.getElementById('receiveAddress');
  const qrWrap = document.getElementById('qrWrap');
  qrWrap.innerHTML = '';

  if (!address) {
    addrEl.innerHTML = '<span class="placeholder">Cüzdan seçin</span>';
    return;
  }

  addrEl.textContent = address;

  try {
    new QRCode(qrWrap, {
      text: address,
      width: 168, height: 168,
      colorDark: '#000', colorLight: '#fff',
      correctLevel: QRCode.CorrectLevel.M,
    });
  } catch {}

  refreshReceive();
}

function copyReceiveAddress() {
  const addr = document.getElementById('receiveAddress').textContent;
  if (addr && addr !== 'Cüzdan seçin') {
    copyToClipboard(addr);
    toast('Adres kopyalandı', 'success');
  }
}

async function refreshReceive() {
  const address = document.getElementById('receiveWalletSelect').value;
  if (!address) return;
  try {
    const b = await get(`/api/wallet/${address}/balance`);
    document.getElementById('receiveBalance').innerHTML =
      `<span class="orange bold">${b.total_sat.toLocaleString()} sat</span>
       <span class="muted"> — ${b.utxo_count} UTXO</span>`;
  } catch {}
}

// ── Send ──────────────────────────────────────────────────────────────────────

async function updateSendBalance() {
  const address = document.getElementById('sendFromSelect').value;
  if (!address) { document.getElementById('sendFromBalance').textContent = ''; return; }
  try {
    const b = await get(`/api/wallet/${address}/balance`);
    document.getElementById('sendFromBalance').textContent =
      `Bakiye: ${b.confirmed_sat.toLocaleString()} sat (onaylanmış)`;
  } catch {}
}

async function buildTx() {
  const from   = document.getElementById('sendFromSelect').value;
  const to     = document.getElementById('sendToAddr').value.trim();
  const amount = parseInt(document.getElementById('sendAmount').value);
  const fee    = parseInt(document.getElementById('sendFee').value) || 500;

  if (!from)   return toast('Kaynak cüzdan seçin', 'error');
  if (!to)     return toast('Alıcı adresi girin', 'error');
  if (!amount || amount < 546) return toast('Miktar en az 546 sat olmalı', 'error');

  try {
    const tx = await post('/api/tx/build', {
      from_address: from, to_address: to,
      amount_sat: amount, fee_sat: fee,
    });

    document.getElementById('txPreview').style.display = 'block';
    document.getElementById('txSize').textContent = `${tx.tx_size} bayt`;
    document.getElementById('txFee').textContent = `${tx.fee_sat.toLocaleString()} sat`;
    document.getElementById('txChange').textContent = tx.change_sat > 0 ? `${tx.change_sat.toLocaleString()} sat` : '—';
    document.getElementById('txSig').textContent = tx.signature.substring(0, 32) + '…';
    document.getElementById('txHex').value = tx.tx_hex;

    // Store hex for broadcast
    document.getElementById('txPreview').dataset.hex = tx.tx_hex;
    toast('Transaction oluşturuldu', 'success');
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function broadcastTx() {
  const txHex = document.getElementById('txHex').value;
  if (!txHex) return;
  const resultEl = document.getElementById('broadcastResult');
  try {
    const r = await post('/api/tx/broadcast', { tx_hex: txHex });
    resultEl.className = 'broadcast-result success';
    resultEl.innerHTML = `✓ Yayınlandı!<br>TXID: <a class="link" href="https://mempool.space/testnet4/tx/${r.txid}" target="_blank">${r.txid}</a>`;
    toast('Transaction yayınlandı!', 'success');
  } catch (e) {
    resultEl.className = 'broadcast-result error';
    resultEl.textContent = `✗ Hata: ${e.message}`;
    toast(e.message, 'error');
  }
}

function copyTxHex() {
  const hex = document.getElementById('txHex').value;
  if (hex) { copyToClipboard(hex); toast('Hex kopyalandı', 'success'); }
}

// ── Transactions ──────────────────────────────────────────────────────────────

async function loadTxHistory() {
  const filterAddr = document.getElementById('txFilterWallet').value;
  const tbody = document.getElementById('txHistoryTable');
  tbody.innerHTML = '<tr><td colspan="6" class="empty">Yükleniyor…</td></tr>';

  let allTxs = [];
  const targets = filterAddr
    ? state.wallets.filter(w => w.address === filterAddr)
    : state.wallets;

  for (const w of targets) {
    try {
      const txs = await get(`/api/wallet/${w.address}/txs`);
      allTxs = allTxs.concat(txs.map(t => ({ ...t, _wallet: w.label, _addr: w.address })));
    } catch {}
  }

  allTxs.sort((a, b) => (b.status?.block_time || 0) - (a.status?.block_time || 0));

  if (!allTxs.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">İşlem bulunamadı</td></tr>';
    return;
  }

  tbody.innerHTML = allTxs.map(tx => {
    const date = tx.status?.block_time
      ? new Date(tx.status.block_time * 1000).toLocaleString('tr-TR')
      : '—';
    const block = tx.status?.block_height ? `#${tx.status.block_height}` : 'Mempool';

    // Basit tutar hesabı
    const outSum = tx.vout?.reduce((s, o) => s + o.value, 0) || 0;

    return `<tr>
      <td><span class="mono-xs truncate" style="max-width:180px;display:inline-block">${tx.txid}</span></td>
      <td class="muted" style="font-size:11px">${date}</td>
      <td><span class="muted">${block}</span></td>
      <td><span class="orange mono-sm">${outSum.toLocaleString()} sat</span></td>
      <td>${statusBadge(tx.status?.confirmed)}</td>
      <td>
        <a class="link" href="https://mempool.space/testnet4/tx/${tx.txid}" target="_blank" title="Explorer'da Aç">↗</a>
      </td>
    </tr>`;
  }).join('');
}

// ── MuSig2 ────────────────────────────────────────────────────────────────────

async function loadMusig2() {
  try {
    state.musig2Sessions = await get('/api/musig2/list');
  } catch {
    state.musig2Sessions = [];
  }
  renderMusig2List();
}

function renderMusig2List() {
  const container = document.getElementById('musig2SessionList');
  if (!state.musig2Sessions.length) {
    container.innerHTML = `<div class="empty-state">Henüz MuSig2 oturumu yok.<br>
      <button class="btn btn-primary" style="margin-top:14px" onclick="openMusig2Modal()">+ Yeni Oturum Oluştur</button></div>`;
    return;
  }

  container.innerHTML = state.musig2Sessions.map(s => {
    const steps = ['KEYS_READY', 'NONCES_READY', 'SIGNING', 'SIGNED', 'BROADCAST'];
    const stepIdx = steps.indexOf(s.state);

    return `<div class="session-card">
      <div class="session-header">
        <div>
          <span class="session-title">${s.label}</span>
          <span class="badge badge-blue" style="margin-left:10px">${s.n}-of-${s.n}</span>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          ${stateBadge(s.state)}
        </div>
      </div>
      <div class="session-body">

        <!-- Step indicator -->
        <div class="step-indicator">
          ${['Anahtar','Nonce','İmza','Yayın'].map((l, i) => `
            <div class="step ${i < stepIdx ? 'done' : i === stepIdx ? 'active' : ''}" title="${l}">${i+1}</div>
            ${i < 3 ? `<div class="step-line ${i < stepIdx ? 'done' : ''}"></div>` : ''}
          `).join('')}
        </div>

        <div class="session-meta">
          <div class="session-meta-item">
            <div>Ağ</div>
            <strong>${s.network}</strong>
          </div>
          <div class="session-meta-item">
            <div>MuSig2 Adresi</div>
            <strong class="mono-sm">${s.agg_address}</strong>
          </div>
          <div class="session-meta-item">
            <div>x-only Pubkey</div>
            <strong class="mono-sm">${s.agg_xonly.substring(0, 20)}…</strong>
          </div>
        </div>

        <div class="participants-grid">
          ${s.participants.map(p => `
            <div class="participant-card">
              <div class="participant-label">${p.label}</div>
              <div class="participant-pk">${p.pk_hex.substring(0, 33)}…</div>
              <div style="margin-top:6px;display:flex;gap:4px">
                ${p.pub_nonce ? '<span class="badge badge-green">Nonce ✓</span>' : '<span class="badge badge-gray">Nonce —</span>'}
              </div>
            </div>
          `).join('')}
        </div>

        <div class="action-row">
          ${s.state === 'KEYS_READY' ? `
            <button class="btn btn-primary" onclick="generateNonces('${s.id}')">Nonce Üret</button>
          ` : ''}
          ${s.state === 'NONCES_READY' ? `
            <button class="btn btn-orange" onclick="openMusig2SignModal('${s.id}')">⚡ İmzala & Yayınla</button>
          ` : ''}
          ${s.state === 'SIGNED' ? `
            <button class="btn btn-orange" onclick="broadcastMusig2('${s.id}')">⚡ Yayınla</button>
            <button class="btn btn-ghost sm" onclick="resetMusig2Nonces('${s.id}')" title="Yeniden imzala">↺ Yeniden İmzala</button>
          ` : ''}
          ${s.state === 'BROADCAST' ? `
            <button class="btn btn-primary" onclick="resetMusig2Nonces('${s.id}')" title="Aynı adres, yeni nonce — yeni işlem imzalamak için">↺ Yeni İşlem</button>
          ` : ''}
          <button class="btn btn-ghost sm" onclick="copyAddr('${s.agg_address}')">⎘ Adres Kopyala</button>
          <a class="btn btn-ghost sm" href="https://mempool.space/testnet4/address/${s.agg_address}" target="_blank">↗ Explorer</a>
        </div>

        ${s.state === 'SIGNED' && s.tx_hex ? `
          <div style="margin-top:12px">
            <label class="form-label">İmzalı TX</label>
            <textarea class="form-textarea mono-sm" readonly rows="2">${s.tx_hex}</textarea>
          </div>
        ` : ''}
      </div>
    </div>`;
  }).join('');
}

async function resetMusig2Nonces(sid) {
  try {
    await post(`/api/musig2/${sid}/nonces`, {});
    uiLog(`MuSig2 ${sid.slice(0,8)}: yeni nonce üretildi, yeni işlem hazır`, 'OK');
    toast('Yeni nonce\'lar üretildi — işlem imzalamaya hazır', 'success');
    await loadMusig2();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function generateNonces(sid) {
  try {
    await post(`/api/musig2/${sid}/nonces`, {});
    toast('Nonce\'lar üretildi ve agrege edildi', 'success');
    await loadMusig2();
  } catch (e) {
    toast(e.message, 'error');
  }
}

function openMusig2SignModal(sid) {
  state.activeMusig2Session = sid;
  document.getElementById('musig2SignResult').style.display = 'none';
  document.getElementById('musig2BroadcastResult').innerHTML = '';
  document.getElementById('musig2SignBtn').textContent = 'İmzala & Yayınla';
  document.getElementById('musig2SignBtn').onclick = () => musig2SignAndBroadcast();

  // Bakiye göster
  const s = state.musig2Sessions.find(s => s.id === sid);
  if (s) {
    document.getElementById('musig2BalConfirmed').textContent = '…';
    document.getElementById('musig2BalUnconfirmed').textContent = '…';
    document.getElementById('musig2BalUtxo').textContent = '…';
    get(`/api/wallet/${s.agg_address}/balance`).then(b => {
      const el = document.getElementById('musig2BalConfirmed');
      el.textContent = `${b.confirmed_sat?.toLocaleString() ?? '—'} sat`;
      el.dataset.sat = b.confirmed_sat ?? 0;
      document.getElementById('musig2BalUnconfirmed').textContent =
        `${b.unconfirmed_sat?.toLocaleString() ?? '—'} sat`;
      document.getElementById('musig2BalUtxo').textContent = b.utxo_count ?? '—';
    }).catch(() => {
      document.getElementById('musig2BalConfirmed').textContent = 'hata';
    });
  }

  openModal('musig2SignModal');
}

function musig2FillMax() {
  const confirmed = parseInt(document.getElementById('musig2BalConfirmed').dataset.sat) || 0;
  const fee = parseInt(document.getElementById('musig2Fee').value) || 500;
  const max = confirmed - fee;
  if (max > 546) {
    document.getElementById('musig2Amount').value = max;
  } else {
    toast('Bakiye yetersiz (ücret dahil)', 'error');
  }
}

async function musig2SignAndBroadcast() {
  const sid = state.activeMusig2Session;
  const to  = document.getElementById('musig2ToAddr').value.trim();
  const amt = parseInt(document.getElementById('musig2Amount').value);
  const fee = parseInt(document.getElementById('musig2Fee').value) || 500;

  if (!to) return toast('Alıcı adres girin', 'error');
  if (!amt || amt < 546) return toast('Miktar en az 546 sat', 'error');

  try {
    const s = state.musig2Sessions.find(s => s.id === sid);
    const r = await post(`/api/musig2/${sid}/sign`, {
      participant_index: 0,
      from_address: s.agg_address,
      to_address: to,
      amount_sat: amt,
      fee_sat: fee,
    });

    document.getElementById('musig2SignResult').style.display = 'block';
    document.getElementById('musig2SigValid').innerHTML =
      r.valid ? '<span class="badge badge-green">✓ GEÇERLİ</span>' : '<span class="badge badge-red">✗ GEÇERSİZ</span>';
    document.getElementById('musig2Sighash').textContent = r.sighash.substring(0, 32) + '…';
    document.getElementById('musig2FinalSig').textContent = r.final_sig.substring(0, 32) + '…';
    document.getElementById('musig2TxHex').value = r.tx_hex;

    // Auto broadcast
    await broadcastMusig2(sid, true);
    await loadMusig2();

    // Broadcast başarılı — butonu "Kapat" olarak değiştir
    const btn = document.getElementById('musig2SignBtn');
    btn.textContent = 'Kapat';
    btn.onclick = () => closeModal('musig2SignModal');
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function broadcastMusig2(sid, fromModal = false) {
  try {
    const r = await post(`/api/musig2/${sid}/broadcast`, {});
    const resultEl = document.getElementById('musig2BroadcastResult');
    if (fromModal) {
      resultEl.className = 'broadcast-result success';
      resultEl.innerHTML = `✓ Yayınlandı!<br>TXID: <a class="link" href="https://mempool.space/testnet4/tx/${r.txid}" target="_blank">${r.txid}</a>`;
    }
    toast('MuSig2 transaction yayınlandı!', 'success');
    await loadMusig2();
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── Wallets Tab ───────────────────────────────────────────────────────────────

function renderWalletsTable() {
  const tbody = document.getElementById('walletsTable');
  if (!state.wallets.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">Cüzdan yok</td></tr>';
    return;
  }
  tbody.innerHTML = state.wallets.map(w => `
    <tr>
      <td><span class="bold">${w.label}</span></td>
      <td><span class="badge ${w.network === 'mainnet' ? 'badge-green' : 'badge-yellow'}">${w.network}</span></td>
      <td>
        <span class="mono-sm truncate" style="max-width:260px;display:inline-block">${w.address}</span>
        <button class="btn btn-ghost sm" onclick="copyAddr('${w.address}')" style="margin-left:4px">⎘</button>
      </td>
      <td><span class="mono-xs truncate" style="max-width:200px;display:inline-block">${w.xonly_pk}</span></td>
      <td>
        <button class="btn btn-ghost sm" onclick="quickReceive('${w.address}')">Al</button>
        <button class="btn btn-ghost sm" onclick="downloadBSMS('${w.label}')" title="Sparrow Wallet için Output Descriptor indir">⬇ Sparrow</button>
        <button class="btn btn-danger sm" onclick="removeWallet('${w.address}')">Sil</button>
      </td>
    </tr>
  `).join('');
}

async function exportWallets() {
  uiLog('Yedek alma başlatıldı');
  try {
    const data = await get('/api/wallet/export');
    if (!data.length) {
      uiLog('Dışa aktarılacak cüzdan yok', 'WARN');
      toast('Dışa aktarılacak cüzdan yok', 'error');
      return;
    }
    uiLog(`${data.length} cüzdan yedeğe alınıyor…`);
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = `taproot-wallets-backup-${new Date().toISOString().slice(0,10)}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    uiLog(`${data.length} cüzdan yedeği indirildi`, 'OK');
    toast(`${data.length} cüzdan yedeği indirildi`, 'success');
  } catch (e) {
    uiLog(`Yedek alma hatası: ${e.message}`, 'ERR');
    toast(e.message, 'error');
  }
}

async function downloadBSMS(label) {
  uiLog(`BSMS export: ${label}`);
  try {
    const encoded = encodeURIComponent(label);
    const res = await fetch(`${API}/api/wallet/export-bsms/${encoded}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const text = await res.text();
    const blob = new Blob([text], { type: 'text/plain' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = label.replace(/\s+/g, '_') + '.descriptor';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    uiLog(`Descriptor indirildi: ${label}.descriptor`, 'OK');
    toast('Descriptor dosyası indirildi', 'success');
    openModal('sparrowGuideModal');
  } catch (e) {
    uiLog(`BSMS export hatası: ${e.message}`, 'ERR');
    toast(e.message, 'error');
  }
}

async function createWallet() {
  const label   = document.getElementById('newWalletLabel').value.trim() || 'Cüzdan';
  const network = document.getElementById('newWalletNetwork').value;
  try {
    await post('/api/wallet/new', { label, network });
    closeModal('newWalletModal');
    await loadWallets();
    toast(`"${label}" cüzdanı oluşturuldu`, 'success');
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function removeWallet(address) {
  if (!confirm('Bu cüzdanı silmek istediğinizden emin misiniz?')) return;
  try {
    await del(`/api/wallet/${address}`);
    await loadWallets();
    toast('Cüzdan silindi', 'success');
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function createMusig2Session() {
  const label   = document.getElementById('musig2Label').value.trim() || 'MuSig2';
  const n       = parseInt(document.getElementById('musig2N').value) || 2;
  const network = document.getElementById('musig2Network').value;
  try {
    await post('/api/musig2/new', { label, n_participants: n, network });
    closeModal('newMusig2Modal');
    await loadMusig2();
    toast(`"${label}" MuSig2 oturumu oluşturuldu`, 'success');
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function populateWalletSelects() {
  const selects = ['receiveWalletSelect', 'sendFromSelect', 'txFilterWallet'];
  selects.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const current = el.value;
    const hasAll  = id === 'txFilterWallet';
    el.innerHTML = (hasAll ? '<option value="">Tüm Cüzdanlar</option>' : '<option value="">— Cüzdan seçin —</option>') +
      state.wallets.map(w => `<option value="${w.address}">${w.label} — ${w.address.substring(0, 16)}…</option>`).join('');
    if (current) el.value = current;
  });
}

function quickReceive(address) {
  document.getElementById('receiveWalletSelect').value = address;
  showTab('receive');
  updateReceive();
}

function quickSend(address) {
  document.getElementById('sendFromSelect').value = address;
  showTab('send');
  updateSendBalance();
}

function copyToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  // HTTP (non-localhost) fallback
  const el = document.createElement('textarea');
  el.value = text;
  el.style.cssText = 'position:fixed;top:-9999px;left:-9999px';
  document.body.appendChild(el);
  el.select();
  document.execCommand('copy');
  document.body.removeChild(el);
  return Promise.resolve();
}

function copyAddr(addr) {
  copyToClipboard(addr);
  toast('Adres kopyalandı', 'success');
}

function statusBadge(confirmed) {
  return confirmed
    ? '<span class="badge badge-green">Onaylandı</span>'
    : '<span class="badge badge-yellow">Bekliyor</span>';
}

function stateBadge(state) {
  const map = {
    KEYS_READY:    ['badge-blue',   'Anahtar Hazır'],
    NONCES_READY:  ['badge-yellow', 'Nonce Hazır'],
    SIGNING:       ['badge-yellow', 'İmzalanıyor'],
    SIGNED:        ['badge-green',  'İmzalandı'],
    BROADCAST:     ['badge-green',  '✓ Yayınlandı'],
  };
  const [cls, label] = map[state] || ['badge-gray', state];
  return `<span class="badge ${cls}">${label}</span>`;
}

// ── Modals ────────────────────────────────────────────────────────────────────

function openModal(id)  { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
function openNewWalletModal()  { openModal('newWalletModal'); }
function openMusig2Modal()     { openModal('newMusig2Modal'); }

// Close modal on overlay click
document.querySelectorAll('.modal-overlay').forEach(overlay => {
  overlay.addEventListener('click', e => {
    if (e.target === overlay) overlay.classList.remove('open');
  });
});

// ── Toast ─────────────────────────────────────────────────────────────────────

function toast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast ${type} show`;
  setTimeout(() => el.classList.remove('show'), 3000);
}
