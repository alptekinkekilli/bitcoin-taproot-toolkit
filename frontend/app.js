/* ═══════════════════════════════════════════════
   Taproot Wallet — Frontend SPA
   ═══════════════════════════════════════════════ */

const DEBUG = false;

const API = '';
let state = {
  wallets: [],
  balances: {},
  txs: {},
  musig2Sessions: [],
  activeMusig2Session: null,
  autoRefresh: true,
  dmusig2Session: null,   // aktif dağıtık MuSig2 oturumu
  myPubkey: null,         // Phase 1: oturumdaki kendi pubkey'im
  myIndex:  null,         // Phase 1: oturumdaki katılımcı indexim
  myRole:   null,         // Phase 1: 'coordinator' | 'participant' | null
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
  if (name === 'musig2d') {
    dLoadSessionList();
    // Phase 3: pubkey biliniyorsa polling'i başlat ve hemen bir tick çalıştır
    if (state.myPubkey) { dStartPolling(); _dPollTick(); }
  }
  // Başka sekmeye geçince polling durdurmaya gerek yok —
  // badge güncellemesi için arka planda devam etmeli.
  // Yalnızca page hidden / component unmount'ta durdurulur.
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
  if (DEBUG) console.log(`[${level}] ${msg}`);
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

// ══════════════════════════════════════════════════════════════════════════════
// Dağıtık MuSig2 — Backend: özel anahtar almaz; yalnızca koordinatördür.
// Tüm secp256k1 + BIP-327 işlemleri musig2d.js (MuSig2D nesnesi) ile yapılır.
// ══════════════════════════════════════════════════════════════════════════════

// Oturum ID'ye göre tarayıcıda saklanan özel anahtar (secretNonce dahil)
const D_SK_KEY     = sid => `dmusig2_sk_${sid}`;
const D_ENC_SK_KEY = sid => `dmusig2_enc_sk_${sid}`;
const D_IDX_KEY    = sid => `dmusig2_idx_${sid}`;
const D_NONCE_KEY  = (sid, inp) => `dmusig2_nonce_${sid}_${inp}`;

// ── Phase 3: Polling state ────────────────────────────────────────────────
let _dPollTimer = null;   // setInterval handle — cleanup için clearInterval gerekli
let _dPolling   = false;  // uçuştaki polling isteği varken yeni tick'i atla (race guard)

// ── Phase 1: PIN tabanlı SK şifreleme ─────────────────────────────────────

async function dEncryptSK(skHex, pin) {
  const enc = new TextEncoder();
  const fromHex = hex => new Uint8Array(hex.match(/.{2}/g).map(b => parseInt(b, 16)));
  const km = await crypto.subtle.importKey('raw', enc.encode(pin), 'PBKDF2', false, ['deriveKey']);
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const key  = await crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt, iterations: 100000, hash: 'SHA-256' },
    km, { name: 'AES-GCM', length: 256 }, false, ['encrypt']
  );
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, enc.encode(skHex));
  const toHex = buf => Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2,'0')).join('');
  return { iv: toHex(iv), salt: toHex(salt), ct: toHex(ct) };
}

async function dDecryptSK(encObj, pin) {
  const enc    = new TextEncoder();
  const fromHex = hex => new Uint8Array(hex.match(/.{2}/g).map(b => parseInt(b, 16)));
  const km = await crypto.subtle.importKey('raw', enc.encode(pin), 'PBKDF2', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt: fromHex(encObj.salt), iterations: 100000, hash: 'SHA-256' },
    km, { name: 'AES-GCM', length: 256 }, false, ['decrypt']
  );
  const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: fromHex(encObj.iv) }, key, fromHex(encObj.ct));
  return new TextDecoder().decode(pt);
}

// SK'yı PIN varsa şifreli, yoksa düz kaydeder
async function dSaveSkWithPin(sid, skHex) {
  const pin = document.getElementById('dmusig2PinInput')?.value || '';
  const hasWebCrypto = typeof crypto !== 'undefined' && !!crypto.subtle;
  if (pin && hasWebCrypto) {
    try {
      const encObj = await dEncryptSK(skHex, pin);
      localStorage.setItem(D_ENC_SK_KEY(sid), JSON.stringify(encObj));
      localStorage.removeItem(D_SK_KEY(sid));  // düz metin kopyasını temizle
      return;
    } catch(e) {
      uiLog(`PIN şifreleme başarısız, düz metin kaydediliyor: ${e.message}`, 'WARN');
    }
  }
  localStorage.setItem(D_SK_KEY(sid), skHex);
  localStorage.removeItem(D_ENC_SK_KEY(sid));
}

// PIN ile şifreli SK'yı çöz ve input'a yaz
async function dUnlockSk() {
  const s = state.dmusig2Session;
  if (!s) return;
  const pin = document.getElementById('dmusig2PinInput')?.value || '';
  if (!pin) { toast('PIN girin', 'error'); return; }
  const encRaw = localStorage.getItem(D_ENC_SK_KEY(s.id));
  if (!encRaw) { toast('Şifreli SK bulunamadı', 'error'); return; }
  try {
    const skHex = await dDecryptSK(JSON.parse(encRaw), pin);
    document.getElementById('dmusig2MySkInput').value = skHex;
    dUpdatePkDisplay();
    toast('SK çözüldü', 'success');
  } catch(_) {
    toast('PIN hatalı veya veri bozuk', 'error');
  }
}

// ── Phase 3: Polling loop ────────────────────────────────────────────────

const D_POLL_INTERVAL = 3000;  // ms

// Aktif aksiyon sayısına göre nav badge + sayfa başlığı + dashboard güncelle
function _dUpdateNavBadge(actions) {
  const ACTIVE = new Set(['build_tx','submit_nonce','submit_partial_sig','broadcast']);
  const count  = actions.filter(a => ACTIVE.has(a.action)).length;
  const badge  = document.getElementById('dmusig2NavBadge');
  if (badge) {
    badge.textContent   = count || '';
    badge.style.display = count ? '' : 'none';
  }
  const base = 'Taproot Wallet';
  document.title = count ? `(${count}) ${base}` : base;
  dRenderDashboard(actions);
}

// ── Phase 4: Aksiyon Kuyruğu Dashboard ────────────────────────────────────

// Phase 5: Kalan süreyi insan okunabilir formata çevirir
function _dFormatExpiry(expiresAt) {
  if (!expiresAt) return null;
  const ms = expiresAt * 1000 - Date.now();
  if (ms <= 0) return 'Süresi doldu';
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  if (h >= 2) return `${h} saat kaldı`;
  if (h === 1) return `1 saat ${m} dk kaldı`;
  return `${m} dk kaldı`;
}

const _D_ACTION_LABELS = {
  build_tx:           '⚡ TX oluştur',
  submit_nonce:       '⚡ Nonce gönder',
  submit_partial_sig: '⚡ Kısmi imza gönder',
  broadcast:          '⚡ Yayınla',
  wait_pubkeys:       '⌛ Diğer pubkey\'ler bekleniyor',
  wait_coordinator:   '⌛ Koordinatör TX hazırlıyor',
  wait_nonce:         '⌛ Diğer nonce\'lar bekleniyor',
  wait_sig:           '⌛ Diğer imzalar bekleniyor',
  done:               '✓ Tamamlandı',
};

function _dParticipantIcons(participants) {
  if (!participants || !participants.length) return '';
  return participants.map(p => {
    const nonceIcon = p.has_nonce ? '<span style="color:#3fb950">✓</span>' : '<span style="color:#484f58">—</span>';
    const sigIcon   = p.has_sig   ? '<span style="color:#3fb950">✓</span>' : '<span style="color:#484f58">—</span>';
    return `<span style="margin-right:10px;font-size:0.82em;white-space:nowrap">${p.label}: ${nonceIcon}nonce ${sigIcon}imza</span>`;
  }).join('');
}

function _dDashCard(entry) {
  const ACTIVE = new Set(['build_tx','submit_nonce','submit_partial_sig','broadcast']);
  const isActive = ACTIVE.has(entry.action);
  const actionLabel = _D_ACTION_LABELS[entry.action] || entry.action;
  const netColor = entry.network === 'mainnet' ? '#f85149' : '#8b949e';

  // Participant satırı — sadece nonce/sig aşamalarında anlamlı
  const showParticipants = ['submit_nonce','submit_partial_sig','wait_nonce','wait_sig']
    .includes(entry.action);
  const participantRow = showParticipants
    ? `<div style="margin-top:6px;color:#8b949e">${_dParticipantIcons(entry.participants)}</div>`
    : '';

  // Phase 5: Kalan süre
  const expiryStr = _dFormatExpiry(entry.expires_at);
  const isExpiring = entry.expires_at && (entry.expires_at * 1000 - Date.now()) < 3_600_000;
  const expiryRow = expiryStr
    ? `<div style="margin-top:4px;font-size:0.78em;color:${isExpiring ? '#f85149' : '#484f58'}">⏱ ${expiryStr}</div>`
    : '';

  const borderColor = isActive ? '#f0883e' : '#30363d';
  const bgColor     = isActive ? '#1a1000' : '#0d1117';

  return `
    <div style="background:${bgColor};border:1px solid ${borderColor};border-radius:8px;padding:12px 14px;margin-bottom:8px;display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap">
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">
          <span style="font-weight:600;font-size:0.9em">${entry.label}</span>
          <span style="font-size:0.75em;color:${netColor}">${entry.network}</span>
          ${dStateBadge(entry.state)}
        </div>
        <div style="color:${isActive ? '#f0883e' : '#8b949e'};font-size:0.85em">${actionLabel}</div>
        ${participantRow}
        ${expiryRow}
      </div>
      <button class="btn btn-ghost sm" onclick="dOpenSession('${entry.session_id}')" style="white-space:nowrap;align-self:center">Aç →</button>
    </div>`;
}

function dRenderDashboard(actions) {
  const dash = document.getElementById('dmusig2Dashboard');
  if (!dash) return;

  // Dashboard yalnızca pubkey biliniyorsa anlamlı
  if (!state.myPubkey || !actions || !actions.length) {
    dash.style.display = 'none';
    return;
  }
  dash.style.display = '';

  const ACTIVE   = new Set(['build_tx','submit_nonce','submit_partial_sig','broadcast']);
  const WAITING  = new Set(['wait_pubkeys','wait_coordinator','wait_nonce','wait_sig']);

  const pending   = actions.filter(a => ACTIVE.has(a.action));
  const watching  = actions.filter(a => WAITING.has(a.action));
  const completed = actions.filter(a => a.action === 'done');

  const pendingEl   = document.getElementById('dmusig2DashPending');
  const watchingEl  = document.getElementById('dmusig2DashWatching');
  const completedEl = document.getElementById('dmusig2DashCompleted');

  pendingEl.innerHTML = pending.length
    ? pending.map(_dDashCard).join('')
    : '<div class="empty-state" style="padding:10px 16px;font-size:0.85em">Bekleyen aksiyon yok.</div>';

  watchingEl.innerHTML = watching.length
    ? watching.map(_dDashCard).join('')
    : '<div class="empty-state" style="padding:10px 16px;font-size:0.85em">İzlenen oturum yok.</div>';

  completedEl.innerHTML = completed.length
    ? completed.map(_dDashCard).join('')
    : '<div class="empty-state" style="padding:10px 16px;font-size:0.85em">Tamamlanan oturum yok.</div>';
}

// Tek polling ticki — _dPolling flag ile race condition engellenir
async function _dPollTick() {
  if (_dPolling) return;              // önceki istek henüz bitmedi, atla
  const pubkey = state.myPubkey;
  if (!pubkey) return;               // pubkey henüz bilinmiyor

  _dPolling = true;
  try {
    const actions = await get(`/api/musig2d/actions?pubkey=${pubkey}`);
    _dUpdateNavBadge(actions);

    // Aktif oturumun state'i veya katılımcı nonce/sig durumu değiştiyse yenile
    const s = state.dmusig2Session;
    if (s) {
      const entry = actions.find(a => a.session_id === s.id);
      if (entry) {
        const stateChanged = entry.state !== s.state;

        // Aynı state içinde nonce/sig gönderildi mi? (participant tablosu diff)
        const participantChanged = (entry.participants || []).some((ep, i) => {
          const sp = s.participants?.[i];
          if (!sp) return true;
          const nonceChanged = ep.has_nonce !== !!(sp.pubnonces && sp.pubnonces.length > 0 &&
                                                   sp.pubnonces[0] !== null);
          const sigChanged   = ep.has_sig   !== !!(sp.partial_sigs && sp.partial_sigs.length > 0 &&
                                                   sp.partial_sigs[0] !== null);
          return nonceChanged || sigChanged;
        });

        if (stateChanged || participantChanged) {
          const updated = await get(`/api/musig2d/${s.id}`);
          state.dmusig2Session = updated;
          dRenderSession(updated);
          uiLog(`Oturum güncellendi: ${s.state} → ${updated.state}${participantChanged && !stateChanged ? ' (katılımcı değişimi)' : ''}`, 'OK');
        }
      }
    }
  } catch(_) {
    // Polling hataları sessizce yutulur — bağlantı kesilince spam yapma
  } finally {
    _dPolling = false;
  }
}

function dStartPolling() {
  if (_dPollTimer !== null) return;  // zaten çalışıyor
  _dPollTimer = setInterval(_dPollTick, D_POLL_INTERVAL);
}

function dStopPolling() {
  if (_dPollTimer === null) return;
  clearInterval(_dPollTimer);
  _dPollTimer = null;
}

// visibilitychange: sekme arka plana geçince durdur, öne gelince devam et
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    dStopPolling();
  } else if (state.myPubkey) {
    // Sayfa görünür oldu + pubkey biliniyor → polling'i yeniden başlat
    dStartPolling();
    _dPollTick();  // hemen bir tick at, interval bekleme
  }
});

function dStateBadge(s) {
  const map = {
    COLLECTING_PUBKEYS: ['badge-yellow', 'Pubkey Bekleniyor'],
    READY_FOR_TX:       ['badge-blue',   'TX Hazır'],
    COLLECTING_NONCES:  ['badge-yellow', 'Nonce Bekleniyor'],
    COLLECTING_SIGS:    ['badge-orange', 'İmza Bekleniyor'],
    SIGNED:             ['badge-green',  'İmzalandı'],
    BROADCAST:          ['badge-green',  '✓ Yayınlandı'],
  };
  const [cls, label] = map[s] || ['badge-gray', s];
  return `<span class="badge ${cls}">${label}</span>`;
}

// ── Oturum Listesi ─────────────────────────────────────────────────────────

async function dLoadSessionList() {
  // Phase 4: pubkey biliniyorsa dashboard'u hemen güncelle
  if (state.myPubkey) {
    get(`/api/musig2d/actions?pubkey=${state.myPubkey}`)
      .then(_dUpdateNavBadge)
      .catch(() => {});
  }
  try {
    const sessions = await get('/api/musig2d/list');
    const el = document.getElementById('dmusig2SessionList');
    if (!sessions.length) {
      el.innerHTML = '<div class="empty-state">Henüz dağıtık MuSig2 oturumu yok.<br>Yeni bir oturum oluşturun.</div>';
      return;
    }
    el.innerHTML = `
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr><th>ID</th><th>Etiket</th><th>N</th><th>Ağ</th><th>Durum</th><th></th></tr></thead>
          <tbody>
            ${sessions.map(s => `
              <tr>
                <td><code style="font-size:0.82em;color:#58a6ff">${s.id}</code></td>
                <td>${s.label}</td>
                <td>${s.n}-of-${s.n}</td>
                <td>${s.network}</td>
                <td>${dStateBadge(s.state)}</td>
                <td>
                  <button class="btn btn-ghost sm" onclick="dOpenSession('${s.id}')">Aç</button>
                  <button class="btn btn-ghost sm" onclick="dDeleteSession('${s.id}')" style="color:#f85149">✕</button>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  } catch(e) {
    uiLog(`Dağıtık MuSig2 listesi yüklenemedi: ${e.message}`, 'ERR');
  }
}

// ── Oturum Oluştur ─────────────────────────────────────────────────────────

function dOpenNewSessionModal() { openModal('dNewSessionModal'); }

async function dCreateSession() {
  const label = document.getElementById('dNewLabel').value.trim() || '2-of-2 Dağıtık';
  const n     = parseInt(document.getElementById('dNewN').value);
  const net   = document.getElementById('dNewNetwork').value;
  try {
    const s = await post('/api/musig2d/new', {label, n_participants: n, network: net});
    closeModal('dNewSessionModal');
    toast('Oturum oluşturuldu', 'success');
    dOpenSession(s.id);
  } catch(e) {
    toast(`Hata: ${e.message}`, 'error');
  }
}

// ── Oturuma Katıl ──────────────────────────────────────────────────────────

async function dJoinSession() {
  const sid = document.getElementById('dJoinSidInput').value.trim();
  if (!sid) { toast('Oturum ID girin', 'error'); return; }
  try {
    await get(`/api/musig2d/${sid}`);
    closeModal('dJoinSessionModal');
    dOpenSession(sid);
  } catch(e) {
    toast(`Oturum bulunamadı: ${e.message}`, 'error');
  }
}

// ── Oturumu Aç / Render ────────────────────────────────────────────────────

async function dOpenSession(sid) {
  try {
    const s = await get(`/api/musig2d/${sid}`);
    state.dmusig2Session = s;

    document.getElementById('dmusig2SessionList').style.display = 'none';
    document.getElementById('dmusig2Detail').style.display = '';

    dRenderSession(s);
    document.querySelector('.page-header button[onclick="dOpenNewSessionModal()"]').style.display = 'none';
    document.querySelector('.page-header button[onclick="openModal(\'dJoinSessionModal\')"]').style.display = 'none';
  } catch(e) {
    toast(`Oturum yüklenemedi: ${e.message}`, 'error');
  }
}

function dCloseDetail() {
  document.getElementById('dmusig2Detail').style.display = 'none';
  document.getElementById('dmusig2SessionList').style.display = '';
  document.querySelector('.page-header button[onclick="dOpenNewSessionModal()"]').style.display = '';
  document.querySelector('.page-header button[onclick="openModal(\'dJoinSessionModal\')"]').style.display = '';
  state.dmusig2Session = null;
  dLoadSessionList();
}

function dRenderSession(s) {
  document.getElementById('dmusig2DetailLabel').textContent = s.label;
  document.getElementById('dmusig2State').textContent       = s.state;
  document.getElementById('dmusig2N').textContent           = `${s.n}-of-${s.n}`;
  document.getElementById('dmusig2Network').textContent     = s.network;
  document.getElementById('dmusig2Sid').textContent         = s.id;

  // Phase 5: Kalan süre gösterimi
  if (s.created_at) {
    const expiresAt  = s.created_at + (48 * 3600);
    const expiryStr  = _dFormatExpiry(expiresAt);
    const isExpiring = (expiresAt * 1000 - Date.now()) < 3_600_000;
    const expiryEl   = document.getElementById('dmusig2Expiry');
    const expiryText = document.getElementById('dmusig2ExpiryText');
    if (expiryEl && expiryText && expiryStr) {
      expiryText.textContent  = expiryStr;
      expiryText.style.color  = isExpiring ? '#f85149' : '#8b949e';
      expiryEl.style.display  = '';
    }
  }

  // Fix 1: Güvenli bağlam uyarısı
  const cryptoStatus = MuSig2D.secureContextStatus();
  document.getElementById('dmusig2CryptoWarning').style.display =
    cryptoStatus.hasWebCrypto ? 'none' : '';

  // Agrege adres kartı — Fix 4: otomatik bakiye çek
  const aggCard = document.getElementById('dmusig2AggCard');
  if (s.agg_address) {
    aggCard.style.display = '';
    document.getElementById('dmusig2AggAddr').textContent = s.agg_address;
    // Fix 4: bakiyeyi her render'da otomatik güncelle
    dRefreshBalance();
  } else {
    aggCard.style.display = 'none';
  }

  // Katılımcı tablosu
  const tbody = document.getElementById('dmusig2ParticipantTable');
  tbody.innerHTML = s.participants.map((p, i) => {
    const pkShort = p.pubkey ? p.pubkey.slice(0,12) + '…' : '—';
    const nonceOk = p.pubnonces && p.pubnonces.length > 0 && p.pubnonces[0] !== null;
    const sigOk   = p.partial_sigs && p.partial_sigs.length > 0 && p.partial_sigs[0] !== null;
    return `<tr>
      <td>${i+1}</td>
      <td>${p.label}</td>
      <td class="mono-sm" style="font-size:0.78em">${pkShort}</td>
      <td>${nonceOk ? '<span style="color:#3fb950">✓</span>' : '—'}</td>
      <td>${sigOk   ? '<span style="color:#3fb950">✓</span>' : '—'}</td>
    </tr>`;
  }).join('');

  // Fix 2: Katılımcı seçici — kayıtlı index'i yükle ve kilitle
  const idxSel = document.getElementById('dmusig2MyIndex');
  const savedIdx = localStorage.getItem(D_IDX_KEY(s.id));
  idxSel.innerHTML = s.participants.map((p, i) =>
    `<option value="${i}">${p.label}</option>`).join('');
  if (savedIdx !== null) {
    idxSel.value = savedIdx;
    idxSel.disabled = true;  // kimlik kilitlendi
    idxSel.title = 'Pubkey kaydedildi — katılımcı kimliği kilitli';
  } else {
    idxSel.disabled = false;
    idxSel.title = '';
  }

  // Phase 1: Rol hesapla (kayıtlı index'e göre)
  if (savedIdx !== null) {
    state.myIndex  = parseInt(savedIdx);
    state.myRole   = state.myIndex === 0 ? 'coordinator' : 'participant';
    state.myPubkey = s.participants[state.myIndex]?.pubkey || null;
  } else {
    state.myIndex  = null;
    state.myRole   = null;
    state.myPubkey = null;
  }

  // Phase 3: pubkey belirlendiyse polling'i (yeniden) başlat
  if (state.myPubkey && !document.hidden) {
    dStartPolling();
  }

  // Mevcut sk'yı yükle (şifreli yoksa) — kullanıcı input'a odaklanmışsa yazmayı atla
  const hasEncSk = !!localStorage.getItem(D_ENC_SK_KEY(s.id));
  const savedSk  = localStorage.getItem(D_SK_KEY(s.id));
  const skInputEl = document.getElementById('dmusig2MySkInput');
  if (savedSk && !hasEncSk && document.activeElement !== skInputEl) {
    skInputEl.value = savedSk;
    dUpdatePkDisplay();
  }

  // Phase 1: Şifreli SK rozeti & kilit açma butonu
  const encSkBadge = document.getElementById('dmusig2EncSkBadge');
  const unlockBtn  = document.getElementById('dmusig2UnlockBtn');
  if (encSkBadge) encSkBadge.style.display = hasEncSk ? '' : 'none';
  if (unlockBtn)  unlockBtn.style.display  = (hasEncSk && !savedSk) ? '' : 'none';

  // Fix 3: TX detay paneli — build-tx sonrası tüm katılımcılara göster
  const txDetails = document.getElementById('dmusig2TxDetails');
  const showDetails = ['COLLECTING_NONCES','COLLECTING_SIGS','SIGNED','BROADCAST'].includes(s.state)
                      && s.to_address;
  if (showDetails) {
    txDetails.style.display = '';
    const amtBtc = (s.amount_sat / 1e8).toFixed(8);
    const feeBtc = (s.fee_sat / 1e8).toFixed(8);
    document.getElementById('dmusig2DetailsDesc').textContent =
      s.description || '(açıklama girilmemiş)';
    document.getElementById('dmusig2DetailsTo').textContent   = s.to_address;
    document.getElementById('dmusig2DetailsAmount').textContent =
      `${s.amount_sat.toLocaleString()} sat  (${amtBtc} BTC)`;
    document.getElementById('dmusig2DetailsFee').textContent  =
      `${s.fee_sat.toLocaleString()} sat  (${feeBtc} BTC)`;
    document.getElementById('dmusig2DetailsChange').textContent =
      s.change_sat > 0 ? `${s.change_sat.toLocaleString()} sat (para üstü — geri gelir)` : 'Yok';
  } else {
    txDetails.style.display = 'none';
  }

  // TX kartı — yalnızca koordinatör görebilir (Phase 1c)
  const isReadyForTx = s.state === 'READY_FOR_TX';
  const isCoordinator = state.myRole === 'coordinator';
  document.getElementById('dmusig2TxCard').style.display =
    (isReadyForTx && isCoordinator) ? '' : 'none';

  // Phase 1c: Koordinatör bekleyici mesajı (participant için)
  const coordWaiting = document.getElementById('dmusig2CoordWaiting');
  if (coordWaiting) {
    coordWaiting.style.display =
      (isReadyForTx && state.myRole === 'participant') ? '' : 'none';
  }

  // İmzalanmış TX kartı
  const signedCard = document.getElementById('dmusig2SignedCard');
  if (s.state === 'SIGNED' || s.state === 'BROADCAST') {
    signedCard.style.display = '';
    document.getElementById('dmusig2TxHex').value = s.tx_hex || '';
  } else {
    signedCard.style.display = 'none';
  }

  // Aksiyon butonları
  dRenderActionButtons(s);
}

function dRenderActionButtons(s) {
  const container = document.getElementById('dmusig2ActionButtons');
  const btns = [];

  // myIndex bilinmiyorsa (henüz pubkey kaydetmemiş) → sadece pubkey kaydet butonu
  const myIdx    = state.myIndex;           // null = bilinmiyor
  const isCoord  = state.myRole === 'coordinator';
  const isKnown  = myIdx !== null;

  if (s.state === 'COLLECTING_PUBKEYS') {
    // Kendi slotu zaten doluysa buton gizle
    const alreadyRegistered = isKnown && s.participants[myIdx]?.pubkey;
    if (!alreadyRegistered) {
      btns.push(`<button class="btn btn-primary" onclick="dRegisterPubkey()">Pubkey Kaydet</button>`);
    }
  }
  if (s.state === 'COLLECTING_NONCES') {
    // Kendi nonce'u yoksa göster
    const hasNonce = isKnown && s.participants[myIdx]?.pubnonces?.length > 0;
    if (!hasNonce) {
      btns.push(`<button class="btn btn-primary" onclick="dSubmitNonce()">Nonce Üret & Gönder</button>`);
    } else {
      btns.push(`<span style="color:#8b949e;font-size:0.85em">⌛ Diğer nonce\'lar bekleniyor</span>`);
    }
  }
  if (s.state === 'COLLECTING_SIGS') {
    // Kendi imzası yoksa göster
    const hasSig = isKnown && s.participants[myIdx]?.partial_sigs?.length > 0;
    if (!hasSig) {
      btns.push(`<button class="btn btn-primary" onclick="dSubmitPartialSig()">Kısmi İmza Üret & Gönder</button>`);
    } else {
      btns.push(`<span style="color:#8b949e;font-size:0.85em">⌛ Diğer imzalar bekleniyor</span>`);
    }
  }
  if (s.state === 'SIGNED') {
    // Phase 2 fix: tüm katılımcılar yayınlayabilir
    btns.push(`<button class="btn btn-orange" onclick="dBroadcast()">⚡ Yayınla</button>`);
  }
  btns.push(`<button class="btn btn-ghost sm" onclick="dRefreshSession()">↺ Güncelle</button>`);

  container.innerHTML = btns.join('');
}

// ── Özel Anahtar Yönetimi ─────────────────────────────────────────────────

async function dGenerateSk() {
  const sk = MuSig2D.generatePrivateKey();
  document.getElementById('dmusig2MySkInput').value = sk;
  const sid = state.dmusig2Session?.id;
  if (sid) await dSaveSkWithPin(sid, sk);
  dUpdatePkDisplay();
  toast('Yeni özel anahtar üretildi', 'success');
}

function dUpdatePkDisplay() {
  const skHex = document.getElementById('dmusig2MySkInput').value.trim();
  const hint  = document.getElementById('dmusig2MyPkDisplay');
  if (!skHex || skHex.length !== 64) { hint.textContent = ''; return; }
  try {
    const pkHex = MuSig2D.derivePublicKey(skHex);
    hint.textContent = `Pubkey: ${pkHex}`;
    hint.style.color = '#3fb950';
    // Oturum açıksa sk'yı kaydet (şifreli kopya yoksa)
    const sid = state.dmusig2Session?.id;
    if (sid && !localStorage.getItem(D_ENC_SK_KEY(sid))) {
      localStorage.setItem(D_SK_KEY(sid), skHex);
    }
  } catch(e) {
    hint.textContent = `Hata: ${e.message}`;
    hint.style.color = '#f85149';
  }
}

// sk input değişince pubkey'i güncelle
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('dmusig2MySkInput').addEventListener('input', dUpdatePkDisplay);
});

// ── Pubkey Kaydet ──────────────────────────────────────────────────────────

async function dRegisterPubkey() {
  const s     = state.dmusig2Session;
  const skHex = document.getElementById('dmusig2MySkInput').value.trim();
  const idx   = parseInt(document.getElementById('dmusig2MyIndex').value);
  if (!s) { toast('Oturum yüklenmedi', 'error'); return; }
  if (!skHex || skHex.length !== 64) { toast('Geçerli özel anahtar girin (64 hex)', 'error'); return; }

  try {
    const pkHex = MuSig2D.derivePublicKey(skHex);
    const updated = await post(`/api/musig2d/${s.id}/register`,
      { participant_index: idx, pubkey_hex: pkHex });
    localStorage.setItem(D_IDX_KEY(s.id), String(idx));
    await dSaveSkWithPin(s.id, skHex);  // PIN varsa şifreli, yoksa düz kaydet
    state.dmusig2Session = updated;
    dRenderSession(updated);
    toast(`Katılımcı ${idx+1} pubkey kaydedildi`, 'success');
  } catch(e) {
    toast(`Pubkey kaydı başarısız: ${e.message}`, 'error');
  }
}

// ── TX Oluştur (koordinatör) ──────────────────────────────────────────────

async function dBuildTx() {
  const s = state.dmusig2Session;
  if (!s) return;
  const to_address = document.getElementById('dmusig2TxTo').value.trim();
  const amount_sat = parseInt(document.getElementById('dmusig2TxAmount').value);
  const fee_sat    = parseInt(document.getElementById('dmusig2TxFee').value) || 500;
  if (!to_address) { toast('Alıcı adres girin', 'error'); return; }
  if (!amount_sat || amount_sat < 546) { toast('Geçerli miktar girin (min 546 sat)', 'error'); return; }

  try {
    const description = document.getElementById('dmusig2TxDesc').value.trim();
    const updated = await post(`/api/musig2d/${s.id}/build-tx`,
      { to_address, amount_sat, fee_sat, description });
    state.dmusig2Session = updated;
    dRenderSession(updated);
    toast(`Sighash hesaplandı — ${updated.sighashes?.length || 0} input`, 'success');
  } catch(e) {
    toast(`TX oluşturma başarısız: ${e.message}`, 'error');
  }
}

// ── Nonce Üret & Gönder ────────────────────────────────────────────────────

async function dSubmitNonce() {
  const s     = state.dmusig2Session;
  const skHex = document.getElementById('dmusig2MySkInput').value.trim();
  const idx   = parseInt(document.getElementById('dmusig2MyIndex').value);
  if (!s) { toast('Oturum yüklenmedi', 'error'); return; }
  if (!skHex || skHex.length !== 64) { toast('Geçerli özel anahtar girin', 'error'); return; }
  if (!s.sighashes || !s.sighashes.length) { toast('Sighash yok — önce TX oluşturun', 'error'); return; }

  try {
    const pkHex = MuSig2D.derivePublicKey(skHex);
    const pubnonces = [];

    for (let i = 0; i < s.sighashes.length; i++) {
      const result = await MuSig2D.nonceGen(skHex, pkHex, s.sighashes[i]);
      // Gizli nonce'u tarayıcıda sakla (partial sign için gerekecek)
      localStorage.setItem(D_NONCE_KEY(s.id, i), JSON.stringify({
        k1: result.secretNonce.k1.toString(),
        k2: result.secretNonce.k2.toString(),
      }));
      // Nonce ile kullanılan sk'yı da kaydet — imzalama adımında tutarlılık için
      await dSaveSkWithPin(s.id, skHex);
      pubnonces.push(result.pubNonce);
    }

    const updated = await post(`/api/musig2d/${s.id}/submit-nonce`,
      { participant_index: idx, pubnonces });
    state.dmusig2Session = updated;
    dRenderSession(updated);
    toast('Nonce gönderildi', 'success');
  } catch(e) {
    toast(`Nonce gönderimi başarısız: ${e.message}`, 'error');
  }
}

// ── Kısmi İmza Üret & Gönder ──────────────────────────────────────────────

async function dSubmitPartialSig() {
  const s     = state.dmusig2Session;
  const skHex = document.getElementById('dmusig2MySkInput').value.trim();
  const idx   = parseInt(document.getElementById('dmusig2MyIndex').value);
  if (!s) { toast('Oturum yüklenmedi', 'error'); return; }
  if (!skHex || skHex.length !== 64) { toast('Geçerli özel anahtar girin', 'error'); return; }
  if (s.state !== 'COLLECTING_SIGS') { toast('Henüz imzalama aşamasında değil', 'error'); return; }

  try {
    const pkHex = MuSig2D.derivePublicKey(skHex);

    // key_agg_coeff hesapla
    const coeff = await MuSig2D.keyAggCoeff(s.pk_list_sorted, pkHex);

    const partial_sigs = [];

    for (let i = 0; i < s.sighashes.length; i++) {
      const nonceRaw = localStorage.getItem(D_NONCE_KEY(s.id, i));
      if (!nonceRaw) {
        toast(`Input ${i} için gizli nonce bulunamadı. Önce nonce üretin.`, 'error');
        return;
      }
      const nonceStored = JSON.parse(nonceRaw);
      const secretNonce = {
        k1: BigInt(nonceStored.k1),
        k2: BigInt(nonceStored.k2),
      };

      const aggNonce = s.agg_nonces[i];   // {r1: hex33, r2: hex33}
      const sighash  = s.sighashes[i];    // 32-byte hex

      // agg_q_even_y: sent by backend; tells us whether key_aggregation Q has even Y.
      // When Q has odd Y, d must be negated so the sig verifies against lift_x(Q.x).
      const qEvenY = s.agg_q_even_y !== false;   // default true for old sessions
      const sigHex = await MuSig2D.partialSign(
        secretNonce, skHex, coeff, s.agg_xonly, aggNonce, sighash, qEvenY
      );
      partial_sigs.push(sigHex);
    }

    const updated = await post(`/api/musig2d/${s.id}/submit-partial-sig`,
      { participant_index: idx, partial_sigs });

    // Phase 5: Nonce'ları yalnızca başarılı response'da sil (finally'de değil)
    // Başarısız imzada nonce kaybolmamalı — kullanıcı tekrar deneyebilmeli
    for (let i = 0; i < s.sighashes.length; i++) {
      localStorage.removeItem(D_NONCE_KEY(s.id, i));
    }

    state.dmusig2Session = updated;
    dRenderSession(updated);

    if (updated.state === 'SIGNED') {
      toast('Tüm imzalar toplandı — TX hazır!', 'success');
    } else {
      toast('Kısmi imza gönderildi', 'success');
    }
  } catch(e) {
    toast(`Kısmi imza başarısız: ${e.message}`, 'error');
    uiLog(`partial_sig hata: ${e.message}`, 'ERR');
    // Nonce'lar localStorage'da kaldı — kullanıcı tekrar imzalayabilir
  }
}

// ── Yayınla ────────────────────────────────────────────────────────────────

async function dBroadcast() {
  const s = state.dmusig2Session;
  if (!s) return;
  const result = document.getElementById('dmusig2BroadcastResult');
  result.textContent = 'Yayınlanıyor...';
  result.className = 'broadcast-result';
  try {
    const res = await post(`/api/musig2d/${s.id}/broadcast`, {});
    result.textContent = `✓ TXID: ${res.txid}`;
    result.className = 'broadcast-result success';
    toast('Transaction yayınlandı!', 'success');
    const updated = await get(`/api/musig2d/${s.id}`);
    state.dmusig2Session = updated;
    dRenderSession(updated);
  } catch(e) {
    result.textContent = `Hata: ${e.message}`;
    result.className = 'broadcast-result error';
  }
}

// ── Yardımcılar ────────────────────────────────────────────────────────────

async function dRefreshSession() {
  const s = state.dmusig2Session;
  if (!s) return;
  try {
    const updated = await get(`/api/musig2d/${s.id}`);
    state.dmusig2Session = updated;
    dRenderSession(updated);
  } catch(e) {
    toast(`Güncelleme başarısız: ${e.message}`, 'error');
  }
}

async function dRefreshBalance() {
  const s = state.dmusig2Session;
  if (!s?.agg_address) return;
  try {
    const b = await get(`/api/wallet/${s.agg_address}/balance`);
    const balText = `${b.confirmed_sat.toLocaleString()} sat onaylı`;
    document.getElementById('dmusig2Balance').textContent = balText;
    const detailsBalEl = document.getElementById('dmusig2DetailsBalance');
    if (detailsBalEl) detailsBalEl.textContent = balText;
  } catch(e) {
    document.getElementById('dmusig2Balance').textContent = 'Bakiye alınamadı';
    const detailsBalEl = document.getElementById('dmusig2DetailsBalance');
    if (detailsBalEl) detailsBalEl.textContent = 'Bakiye alınamadı';
  }
}

async function dDeleteSession(sid) {
  if (!confirm('Bu oturumu silmek istediğinize emin misiniz?')) return;
  try {
    await del(`/api/musig2d/${sid}`);
    toast('Oturum silindi', 'success');
    dLoadSessionList();
  } catch(e) {
    toast(`Silme başarısız: ${e.message}`, 'error');
  }
}

function dCopySid() {
  const sid = state.dmusig2Session?.id;
  if (sid) { copyToClipboard(sid); toast('Oturum ID kopyalandı', 'success'); }
}

function dCopyAggAddr() {
  const addr = state.dmusig2Session?.agg_address;
  if (addr) { copyToClipboard(addr); toast('Adres kopyalandı', 'success'); }
}

function dCopyTxHex() {
  const hex = document.getElementById('dmusig2TxHex').value;
  if (hex) { copyToClipboard(hex); toast('TX hex kopyalandı', 'success'); }
}
