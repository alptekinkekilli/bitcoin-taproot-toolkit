/* ═══════════════════════════════════════════════
   Taproot Wallet — Frontend SPA
   ═══════════════════════════════════════════════ */

const DEBUG = false;

const API = '';
let state = {
  wallets: [],
  balances: {},
  txs: {},
  hdScanResults: {},      // walletId → { addresses, total_balance_sat, scanned }
  hdPanelOpen: {},        // walletId → bool (panel açık mı?)
  txLastScan: {},         // address → ISO timestamp
  musig2Sessions: [],
  dmusig2Sessions: [],    // dağıtık MuSig2 aktif session listesi (dashboard/dropdown için)
  dmusig2SessionsAll: [], // tüm dMusig2 session'ları (arşiv dahil — TX geçmişi için)
  activeMusig2Session: null,
  autoRefresh: true,
  dmusig2Session: null,
  myPubkey: null,
  myIndex:  null,
  myRole:   null,
  archiveSessions: [],
  archiveFiltered: [],
  archivePage: 1,
  archivePageSize: 25,
  _archiveOpenedDetail: false,
  dmusig2SubTab: 'active',
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
  // 60s'de bir sadece bakiye + cüzdan listesi yenile (TX geçmişi dahil değil)
  setInterval(() => { if (state.autoRefresh) refreshAll(); }, 60000);
});

// ── Navigation ────────────────────────────────────────────────────────────────

function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(t => t.classList.remove('active'));
  document.getElementById(`tab-${name}`).classList.add('active');
  document.querySelector(`[data-tab="${name}"]`).classList.add('active');

  if (name === 'dashboard') refreshDashboard();
  if (name === 'addresses') loadAddresses();
  if (name === 'transactions') {
    txShowTab(txState.tab || 'wallets');
  }
  if (name === 'musig2') loadMusig2();
  if (name === 'musig2d') {
    // Detay paneli açık kalmışsa temizle
    const detail = document.getElementById('dmusig2Detail');
    if (detail && detail.style.display !== 'none') {
      detail.style.display = 'none';
      state.dmusig2Session = null;
      state._archiveOpenedDetail = false;
    }

    // Header butonlarını her zaman restore et
    const newBtn  = document.querySelector('.page-header button[onclick="dOpenNewSessionModal()"]');
    const joinBtn = document.querySelector('.page-header button[onclick="openModal(\'dJoinSessionModal\')"]');
    if (newBtn)  newBtn.style.display  = '';
    if (joinBtn) joinBtn.style.display = '';

    // Dışarıdan bu sekmeye dönüldüğünde Aktif tab'ı varsayılan yap
    state.dmusig2SubTab = 'active';
    dShowSubTab('active');

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
  // dMusig2 session'larını yükle
  get('/api/musig2d/list').then(all => {
    state.dmusig2SessionsAll = all;  // TX geçmişi için tümü
    state.dmusig2Sessions = all.filter(s => s.state !== 'BROADCAST');
    populateWalletSelects();
  }).catch(() => {});
}

async function loadWallets() {
  state.wallets = await get('/api/wallet/list').catch(() => []);
  // G1/G2: backend'deki hd_addresses'i state.hdScanResults'a yükle
  state.wallets.forEach(w => {
    if (w.hd_addresses && Object.keys(w.hd_addresses).length) {
      const addrs = Object.values(w.hd_addresses);
      if (!state.hdScanResults[w.id]) {
        state.hdScanResults[w.id] = {
          addresses: addrs.map(a => ({ ...a })),
          total_balance_sat: addrs.reduce((s, a) => s + (a.balance_sat || 0), 0),
          scanned: addrs.length,
        };
      }
    }
  });
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
  // 2B: Tüm birincil adreslerin bakiyesini çek
  const balFetches = state.wallets.map(async w => {
    try {
      const b = await get(`/api/wallet/${w.address}/balance`);
      state.balances[w.address] = b;
      return b;
    } catch { return null; }
  });

  // HD alt adresler için bakiye çek — sadece daha önce bakiyesi olan veya
  // henüz hiç sorgulanmamış adresler (her döngüde 46 istek atmaktan kaçın)
  const hdFetches = [];
  state.wallets.forEach(w => {
    const scan = state.hdScanResults[w.id];
    if (!scan) return;
    scan.addresses.forEach(a => {
      if (!a.address || a.address === w.address) return;
      // Bakiyesi sıfır ve daha önce sorgulanmışsa yeniden sorgulamaya gerek yok
      const cached = state.balances[a.address];
      if (cached && cached.total_sat === 0 && cached.utxo_count === 0) return;
      hdFetches.push(get(`/api/wallet/${a.address}/balance`).then(b => {
        state.balances[a.address] = b;
        a.balance_sat = b.total_sat;
        a.utxo_count  = b.utxo_count;
      }).catch(() => {}));
    });
  });

  await Promise.all([...balFetches, ...hdFetches]);

  // 2B: Toplam = birincil + HD alt adresleri
  let totalSat = 0, confirmedSat = 0, unconfirmedSat = 0;
  state.wallets.forEach(w => {
    const b = state.balances[w.address];
    if (b) { totalSat += b.total_sat; confirmedSat += b.confirmed_sat; unconfirmedSat += b.unconfirmed_sat; }
    const scan = state.hdScanResults[w.id];
    if (scan) {
      scan.addresses.forEach(a => {
        if (a.address === w.address) return;
        const hb = state.balances[a.address];
        if (hb) { totalSat += hb.total_sat; confirmedSat += hb.confirmed_sat; unconfirmedSat += hb.unconfirmed_sat; }
      });
    }
  });

  document.getElementById('totalBalance').textContent    = `${totalSat.toLocaleString()} sat`;
  document.getElementById('totalBalanceBtc').textContent = `${(totalSat / 1e8).toFixed(8)} BTC`;
  document.getElementById('confirmedBalance').textContent   = `${confirmedSat.toLocaleString()} sat`;
  document.getElementById('unconfirmedBalance').textContent = `${unconfirmedSat.toLocaleString()} sat`;

  // 2C: Cüzdan Sayısı kutusu — "N cüzdan / K aktif adres"
  const activeAddrCount = (() => {
    const seen = new Set();
    state.wallets.forEach(w => {
      const b = state.balances[w.address];
      if (b && b.total_sat > 0) seen.add(w.address);
      const scan = state.hdScanResults[w.id];
      if (scan) scan.addresses.forEach(a => {
        if (a.balance_sat > 0) seen.add(a.address);
      });
    });
    return seen.size;
  })();
  document.getElementById('walletCount').textContent = state.wallets.length;
  const walletSubEl = document.getElementById('walletCountSub');
  if (walletSubEl) walletSubEl.textContent = activeAddrCount ? `${activeAddrCount} aktif adres` : '';

  // 2A: Adresler tablosu — birincil + HD alt satırları
  _renderDashboardAddressTable();

  await loadRecentTxs();
}

function _renderDashboardAddressTable() {
  const tbody = document.getElementById('dashboardWalletTable');
  if (!state.wallets.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">Cüzdan yok — Cüzdanlar sekmesinden ekleyin</td></tr>';
    return;
  }

  let rows = '';
  state.wallets.forEach(w => {
    const b = state.balances[w.address];
    const satDisplay  = b ? `${b.total_sat.toLocaleString()} sat` : '…';
    const utxoDisplay = b ? b.utxo_count : '—';
    const scan = state.hdScanResults[w.id];
    const hasHD = scan && scan.addresses.some(a => a.address !== w.address && (a.balance_sat > 0 || a.utxo_count > 0));
    const toggleBtn = hasHD
      ? `<button class="btn btn-ghost sm" onclick="_toggleDashHD('${w.id}')" id="dash-hd-toggle-${w.id}" title="HD alt adresleri göster">▶</button>`
      : '';

    rows += `<tr>
      <td><span class="bold">${w.label}</span>${w.hd_imported ? ' <span class="badge badge-yellow" style="font-size:9px">import</span>' : ''}${toggleBtn}</td>
      <td><span class="mono-xs truncate" style="max-width:220px;display:inline-block">${w.address}</span></td>
      <td><span class="orange bold">${satDisplay}</span></td>
      <td>${utxoDisplay}</td>
      <td>
        <button class="btn btn-ghost sm" onclick="quickReceive('${w.address}')">Al</button>
        <button class="btn btn-ghost sm" onclick="quickSend('${w.address}')">Gönder</button>
      </td>
    </tr>`;

    // 2A: HD alt adres satırları (toggle ile açılıp kapanır)
    if (hasHD) {
      const open = state.hdPanelOpen[`dash-${w.id}`];
      const activeHD = scan.addresses.filter(a => a.address !== w.address && (a.balance_sat > 0 || a.utxo_count > 0));
      rows += `<tr id="dash-hd-rows-${w.id}" style="display:${open ? '' : 'none'}">
        <td colspan="5" style="padding:0">
          <table class="data-table" style="font-size:12px;background:var(--bg-2)">
            <tbody>
              ${activeHD.map(a => {
                const hb = state.balances[a.address];
                const hSat  = hb ? hb.total_sat.toLocaleString() : (a.balance_sat || 0).toLocaleString();
                const hUtxo = hb ? hb.utxo_count : (a.utxo_count || 0);
                return `<tr style="border-left:3px solid var(--orange)">
                  <td style="padding-left:24px;color:var(--text-3)">${w.label} [${a.index ?? '?'}]</td>
                  <td><span class="mono-xs" style="word-break:break-all">${a.address}</span></td>
                  <td><span class="orange bold">${hSat} sat</span></td>
                  <td>${hUtxo}</td>
                  <td>
                    <button class="btn btn-ghost sm" onclick="quickReceive('${a.address}')">Al</button>
                    <button class="btn btn-ghost sm" onclick="quickSend('${a.address}')">Gönder</button>
                  </td>
                </tr>`;
              }).join('')}
            </tbody>
          </table>
        </td>
      </tr>`;
    }
  });

  tbody.innerHTML = rows;
}

function _toggleDashHD(walletId) {
  const key = `dash-${walletId}`;
  state.hdPanelOpen[key] = !state.hdPanelOpen[key];
  const row = document.getElementById(`dash-hd-rows-${walletId}`);
  const btn = document.getElementById(`dash-hd-toggle-${walletId}`);
  if (row) row.style.display = state.hdPanelOpen[key] ? '' : 'none';
  if (btn) btn.textContent = state.hdPanelOpen[key] ? '▼' : '▶';
}

async function loadRecentTxs() {
  const tbody = document.getElementById('recentTxTable');
  let allTxs = [];

  // Birincil adresler + bakiyesi OLAN veya harcama yaşamış HD sub-adresler
  // (bakiyesi sıfır ve hiç TX geçmişi olmayan adresleri dashboard'a yükleme)
  const targets = [];
  for (const w of state.wallets) {
    targets.push({ addr: w.address, label: w.label, network: w.network });
    Object.entries(w.hd_addresses || {}).forEach(([idx, info]) => {
      if (!info.address || info.address === w.address) return;
      // Bakiyesi olan VEYA daha önce funded olmuş (balance=0 ama tx geçmişi var)
      const hadActivity = (info.balance_sat || 0) > 0 || (info.utxo_count || 0) > 0
        || state.balances[info.address]?.total_sat > 0
        || (state.balances[info.address] === undefined);  // ilk açılışta tümünü bir kez çek
      if (hadActivity)
        targets.push({ addr: info.address, label: `${w.label} [${idx}]`, network: w.network });
    });
  }

  await Promise.all(targets.map(async t => {
    try {
      const txs = await get(`/api/wallet/${t.addr}/txs`);
      txs.slice(0, 5).forEach(tx => allTxs.push({ ...tx, _wallet: t.label, _network: t.network }));
    } catch {}
  }));

  // Unconfirmed en üste (block_time yoksa Infinity gibi davran)
  const sortKey = tx => tx.status?.confirmed ? (tx.status?.block_time || 0) : Infinity;
  allTxs.sort((a, b) => sortKey(b) - sortKey(a));

  // Deduplicate by txid
  const seen = new Set();
  allTxs = allTxs.filter(tx => { if (seen.has(tx.txid)) return false; seen.add(tx.txid); return true; });

  if (!allTxs.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty">İşlem yok</td></tr>';
    return;
  }

  const explorerBase = n => n === 'mainnet' ? 'https://mempool.space' : 'https://mempool.space/testnet4';
  tbody.innerHTML = allTxs.slice(0, 10).map(tx => {
    const outSum = tx.vout?.reduce((s, o) => s + o.value, 0) || 0;
    return `<tr>
      <td>
        <span class="bold" style="font-size:11px;color:var(--text-3)">${tx._wallet}</span><br>
        <span class="mono-xs">${tx.txid.substring(0, 20)}…</span>
        <button class="btn btn-ghost sm" style="padding:1px 4px;font-size:10px" onclick="copyToClipboard('${tx.txid}');toast('TXID kopyalandı','success')">⎘</button>
      </td>
      <td>${tx.status?.block_height ? `#${tx.status.block_height}` : '<span style="color:var(--text-3)">Mempool</span>'}</td>
      <td><span class="orange" style="font-size:12px">${outSum.toLocaleString()} sat</span></td>
      <td>${statusBadge(tx.status?.confirmed)}</td>
      <td><a class="link" href="${explorerBase(tx._network)}/tx/${tx.txid}" target="_blank">↗</a></td>
    </tr>`;
  }).join('');
}

// ── Receive ───────────────────────────────────────────────────────────────────

async function updateReceive() {
  const walletId = document.getElementById('receiveWalletSelect').value;
  const addrEl = document.getElementById('receiveAddress');
  const qrWrap = document.getElementById('qrWrap');
  qrWrap.innerHTML = '';

  if (!walletId) {
    addrEl.innerHTML = '<span class="placeholder">Cüzdan seçin</span>';
    document.getElementById('receiveBalance').innerHTML = '';
    return;
  }

  const w = state.wallets.find(x => x.id === walletId);
  let address = w ? w.address : '';

  // HD cüzdanlarda fresh address kullan (BIP-44/86 adres yeniden kullanımını önler)
  if (w && w.hd && w.hd_addresses && Object.keys(w.hd_addresses).length > 0) {
    addrEl.innerHTML = '<span class="placeholder">⏳ Taze adres aranıyor…</span>';
    try {
      const fresh = await get(`/api/wallet/${walletId}/fresh-address`);
      address = fresh.address;
      if (fresh.all_used) {
        toast('Tüm adresler kullanılmış — son adres gösteriliyor', 'warning');
      }
    } catch { /* fallback to primary */ }
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
  if (addr && addr !== 'Cüzdan seçin' && !addr.includes('⏳')) {
    copyToClipboard(addr);
    toast('Adres kopyalandı', 'success');
  }
}

async function refreshReceive() {
  // Gösterilen adresten bakiye çek (select value artık wallet ID)
  const addr = document.getElementById('receiveAddress').textContent.trim();
  if (!addr || addr.startsWith('Cüzdan') || addr.startsWith('⏳')) return;
  try {
    const b = await get(`/api/wallet/${addr}/balance`);
    document.getElementById('receiveBalance').innerHTML =
      `<span class="orange bold">${b.total_sat.toLocaleString()} sat</span>
       <span class="muted"> — ${b.utxo_count} UTXO</span>`;
  } catch {}
}

// ── Addresses Tab ─────────────────────────────────────────────────────────────

async function loadAddresses() {
  const walletId = document.getElementById('addrWalletSelect').value;
  const content  = document.getElementById('addrTabContent');
  if (!walletId) {
    content.innerHTML = '<div style="color:var(--text-3);padding:24px 0;text-align:center">Cüzdan seçin</div>';
    return;
  }

  content.innerHTML = '<div style="color:var(--text-3);padding:24px 0;text-align:center">⏳ Adresler yükleniyor…</div>';
  try {
    const data = await get(`/api/wallet/${walletId}/addresses`);
    if (!data.receive.length && !data.change.length) {
      content.innerHTML = `
        <div style="color:var(--text-3);padding:24px;text-align:center">
          Bu cüzdan için adres türetilmemiş.
          <br><br>
          <button class="btn btn-primary" onclick="generateAddresses('${walletId}')">Adres Türet (20×2)</button>
        </div>`;
      return;
    }
    renderAddressesTab(data);
  } catch (e) {
    content.innerHTML = `<div style="color:var(--red);padding:16px">Hata: ${e.message}</div>`;
  }
}

async function generateAddresses(walletId) {
  try {
    await post(`/api/wallet/${walletId}/generate-addresses`, {});
    toast('Adresler türetildi', 'success');
    // Wallets listesini güncelle (hd_addresses içermesi için)
    state.wallets = await get('/api/wallet/list');
    loadAddresses();
  } catch (e) {
    toast(`Hata: ${e.message}`, 'error');
  }
}

function renderAddressesTab(data) {
  const content = document.getElementById('addrTabContent');

  function addrTable(title, rows, isChange) {
    let html = `
      <div class="section-header" style="margin-top:${isChange ? '24px' : '0'}">
        <h2>${title}</h2>
        <span class="muted" style="font-size:13px">${rows.length} adres</span>
      </div>
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr>
            <th>#</th><th>Adres</th><th>Bakiye</th><th>UTXO</th><th></th>
          </tr></thead>
          <tbody>`;
    rows.forEach(r => {
      const used  = r.balance_sat > 0 || r.utxo_count > 0;
      const style = used ? 'color:var(--orange)' : '';
      const shortAddr = r.address.substring(0, 20) + '…' + r.address.slice(-8);
      const balText = r.balance_sat > 0 ? `${r.balance_sat.toLocaleString()} sat` : '<span class="muted">0</span>';
      html += `<tr>
        <td style="${style}">${r.index}</td>
        <td style="font-family:monospace;font-size:12px;${style}" title="${r.address}">${shortAddr}</td>
        <td>${balText}</td>
        <td>${r.utxo_count || '<span class="muted">0</span>'}</td>
        <td><button class="btn btn-ghost sm" onclick="copyToClipboard('${r.address}');toast('Kopyalandı','success')" title="Kopyala">⎘</button></td>
      </tr>`;
    });
    if (!rows.length) {
      html += `<tr><td colspan="5" class="empty">Adres yok — cüzdan yeni oluşturulmuşsa yeniden oluşturun</td></tr>`;
    }
    html += '</tbody></table></div>';
    return html;
  }

  content.innerHTML =
    addrTable('Receive Adresleri (m/86\'/…/0/*)', data.receive || [], false) +
    addrTable('Change Adresleri (m/86\'/…/1/*)',  data.change  || [], true);
}

// ── Send ──────────────────────────────────────────────────────────────────────

// Coin-control state
const sendState = {
  utxos: [],           // fetch edilmiş tüm onaylı UTXO'lar
  selected: new Set(), // seçili "txid:vout" stringleri
  preSelect: null,     // quickSend'den gelen pre-seçim
};

async function updateSendBalance() {
  const address = document.getElementById('sendFromSelect').value;
  const panel   = document.getElementById('sendUtxoPanel');
  const listEl  = document.getElementById('sendUtxoList');
  const balEl   = document.getElementById('sendFromBalance');

  if (!address) {
    if (balEl) balEl.textContent = '';
    if (panel) panel.style.display = 'none';
    return;
  }

  // Hangi cüzdana ait? Birincil mi yoksa HD alt-adres mi?
  let ownerWallet  = null;
  let isPrimary    = false;
  for (const w of state.wallets) {
    if (w.address === address) { ownerWallet = w; isPrimary = true; break; }
    const hdAddrs = w.hd_addresses || {};
    if (Object.values(hdAddrs).some(info => info.address === address)) {
      ownerWallet = w; break;
    }
  }

  // Bakiye göster
  try {
    const b = await get(`/api/wallet/${address}/balance`);
    if (balEl) balEl.textContent = `Bakiye: ${b.confirmed_sat.toLocaleString()} sat (onaylanmış)`;
  } catch {}

  // UTXO paneli aç
  if (panel) panel.style.display = '';
  if (listEl) listEl.innerHTML = '<span style="color:var(--text-3)">UTXO\'lar yükleniyor…</span>';

  sendState.utxos    = [];
  sendState.selected = new Set();
  sendState.ownerWallet = ownerWallet;

  try {
    // Hangi adresleri tarayacağız?
    // Eğer birincil adres seçiliyse ve HD cüzdansa → tüm aktif alt-adresleri de tara
    const targets = [];  // [{addr, label}]
    if (ownerWallet && isPrimary && ownerWallet.hd) {
      const hdAddrs = ownerWallet.hd_addresses || {};
      const active  = Object.entries(hdAddrs).filter(([, info]) => info.balance_sat > 0 || info.utxo_count > 0);
      if (active.length) {
        // Birincil adres: index 0 veya doğrudan w.address
        targets.push({ addr: ownerWallet.address, label: `${ownerWallet.label} (birincil)` });
        active.forEach(([idx, info]) => targets.push({ addr: info.address, label: `${ownerWallet.label} [${idx}]` }));
      } else {
        targets.push({ addr: address, label: ownerWallet?.label || address.substring(0, 14) + '…' });
      }
    } else {
      const subLabel = ownerWallet ? (() => {
        const idx = Object.entries(ownerWallet.hd_addresses || {}).find(([, i]) => i.address === address)?.[0];
        return idx ? `${ownerWallet.label} [${idx}]` : ownerWallet.label;
      })() : address.substring(0, 14) + '…';
      targets.push({ addr: address, label: subLabel });
    }

    // Paralel UTXO çekimi
    const results = await Promise.all(targets.map(async t => {
      try {
        const utxos = await get(`/api/wallet/${t.addr}/utxos`);
        return (utxos || []).filter(u => (u.confirmations || 0) >= 1)
          .map(u => ({ ...u, _addr: t.addr, _label: t.label }));
      } catch { return []; }
    }));

    let all = results.flat();
    all.sort((a, b) => a.value - b.value);  // smallest-first
    sendState.utxos = all;

    // Pre-seçim (quickSend ile geldi)
    if (sendState.preSelect) {
      sendState.selected.add(sendState.preSelect);
      sendState.preSelect = null;
    }

    _renderSendUtxoList();
  } catch {
    if (listEl) listEl.innerHTML = '<span style="color:#f85149">UTXO yüklenemedi.</span>';
  }
}

function _renderSendUtxoList() {
  const listEl   = document.getElementById('sendUtxoList');
  const summaryEl = document.getElementById('sendUtxoSummary');
  if (!listEl) return;

  const utxos = sendState.utxos;
  if (!utxos.length) {
    listEl.innerHTML = '<span style="color:var(--text-3)">Onaylanmış UTXO bulunamadı.</span>';
    if (summaryEl) summaryEl.textContent = '';
    return;
  }

  const selTotal = utxos
    .filter(u => sendState.selected.has(`${u.txid}:${u.vout}`))
    .reduce((s, u) => s + u.value, 0);

  if (summaryEl) {
    summaryEl.textContent = sendState.selected.size
      ? `${sendState.selected.size} seçili — ${selTotal.toLocaleString()} sat`
      : `${utxos.length} UTXO — Otomatik seçim (miktar yetene kadar)`;
  }

  const multiAddr = new Set(utxos.map(u => u._addr)).size > 1;
  listEl.innerHTML = `
    <div class="table-wrap" style="max-height:260px;overflow-y:auto">
      <table class="data-table" style="font-size:12px">
        <thead><tr>
          <th style="width:24px"></th>
          ${multiAddr ? '<th>Adres</th>' : ''}
          <th>TXID:vout</th>
          <th>Tutar</th>
          <th>Onay</th>
        </tr></thead>
        <tbody>${utxos.map(u => {
          const id  = `${u.txid}:${u.vout}`;
          const chk = sendState.selected.has(id) ? 'checked' : '';
          const txShort = u.txid.substring(0, 16) + '…:' + u.vout;
          return `<tr style="cursor:pointer" onclick="sendToggleUtxo('${id}')">
            <td><input type="checkbox" ${chk} onclick="event.stopPropagation();sendToggleUtxo('${id}')"></td>
            ${multiAddr ? `<td><span style="color:var(--text-3);font-size:11px">${u._label || ''}</span></td>` : ''}
            <td><span class="mono-xs">${txShort}</span></td>
            <td><span class="orange">${u.value.toLocaleString()} sat</span></td>
            <td>${u.confirmations >= 1 ? `<span class="badge badge-green">${u.confirmations}</span>` : '<span class="badge badge-yellow">0</span>'}</td>
          </tr>`;
        }).join('')}</tbody>
      </table>
    </div>`;
}

function sendToggleUtxo(id) {
  if (sendState.selected.has(id)) sendState.selected.delete(id);
  else sendState.selected.add(id);
  _renderSendUtxoList();
}

function sendSelectAllUtxos() {
  sendState.utxos.forEach(u => sendState.selected.add(`${u.txid}:${u.vout}`));
  _renderSendUtxoList();
}

function sendClearUtxoSel() {
  sendState.selected.clear();
  _renderSendUtxoList();
}

async function buildTx() {
  const from   = document.getElementById('sendFromSelect').value;
  const to     = document.getElementById('sendToAddr').value.trim();
  const amount = parseInt(document.getElementById('sendAmount').value);
  const fee    = parseInt(document.getElementById('sendFee').value) || 500;

  if (!from)   return toast('Kaynak cüzdan seçin', 'error');
  if (!to)     return toast('Alıcı adresi girin', 'error');
  if (!amount || amount < 546) return toast('Miktar en az 546 sat olmalı', 'error');

  const buildBtn = document.querySelector('button[onclick="buildTx()"]');
  if (buildBtn) { buildBtn.disabled = true; buildBtn.textContent = '⏳ Lütfen bekleyin…'; }

  const body = { from_address: from, to_address: to, amount_sat: amount, fee_sat: fee };
  if (sendState.selected.size > 0) body.utxo_ids = [...sendState.selected];

  try {
    const tx = await post('/api/tx/build', body);

    document.getElementById('txPreview').style.display = 'block';
    document.getElementById('txSize').textContent = `${tx.tx_size} bayt`;
    document.getElementById('txFee').textContent = `${tx.fee_sat.toLocaleString()} sat`;
    document.getElementById('txSendAmount').textContent = `${amount.toLocaleString()} sat`;
    document.getElementById('txChange').textContent = tx.change_sat > 0 ? `${tx.change_sat.toLocaleString()} sat` : '—';
    document.getElementById('txSig').textContent = tx.signature.substring(0, 32) + '…';
    document.getElementById('txHex').value = tx.tx_hex;
    document.getElementById('txPreview').dataset.hex = tx.tx_hex;

    // Kullanılan UTXO'ları önizlemede göster
    const usedEl = document.getElementById('txUsedUtxoList');
    if (usedEl && tx.used_utxos?.length) {
      // Lokal state'teki UTXO'larla eşleştirip adres etiketini bul
      const utxoMap = {};
      sendState.utxos.forEach(u => { utxoMap[`${u.txid}:${u.vout}`] = u; });
      const totalIn = tx.used_utxos.reduce((s, u) => s + u.value, 0);
      usedEl.innerHTML = `
        <div style="margin-bottom:6px;color:var(--text-2)">
          ${tx.used_utxos.length} UTXO — Toplam giriş: <strong style="color:var(--orange)">${totalIn.toLocaleString()} sat</strong>
        </div>
        <div class="table-wrap">
          <table class="data-table" style="font-size:12px">
            <thead><tr><th>TXID:vout</th><th>Adres</th><th>Tutar</th></tr></thead>
            <tbody>${tx.used_utxos.map(u => {
              const local = utxoMap[u.id];
              const label = local?._label || '';
              const short = u.txid.substring(0, 16) + '…:' + u.vout;
              return `<tr>
                <td><span class="mono-xs">${short}</span></td>
                <td><span style="color:var(--text-3);font-size:11px">${label}</span></td>
                <td><span class="orange">${u.value.toLocaleString()} sat</span></td>
              </tr>`;
            }).join('')}</tbody>
          </table>
        </div>`;
    }

    document.getElementById('broadcastResult').innerHTML = '';
    document.getElementById('broadcastResult').className = 'broadcast-result';
    toast('Transaction oluşturuldu — UTXO listesini kontrol edin, ardından Onayla ve Yayınla', 'success');
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    if (buildBtn) { buildBtn.disabled = false; buildBtn.textContent = 'Transaction Oluştur'; }
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

// ── Transactions (3-tab sistem) ───────────────────────────────────────────────

// State
const txState = {
  tab: 'wallets',
  // Cüzdanlar
  wTxs: [], wFiltered: [], wPage: 1, wPageSize: 25,
  // MuSig2
  m2Sessions: [], m2Filtered: [], m2Page: 1, m2PageSize: 25,
  // Dağıtık MuSig2
  dm2Sessions: [], dm2Filtered: [], dm2Page: 1, dm2PageSize: 25,
};

function txShowTab(tab) {
  txState.tab = tab;
  ['wallets','musig2','dmusig2'].forEach(t => {
    const pane = document.getElementById(`txPane-${t}`);
    const btn  = document.getElementById(`txTab-${t}`);
    const active = t === tab;
    if (pane) pane.style.display = active ? '' : 'none';
    if (btn) {
      btn.style.borderBottomColor = active ? 'var(--orange)' : 'transparent';
      btn.style.color = active ? 'var(--orange)' : 'var(--text-2)';
    }
  });
  if (tab === 'wallets')  txwLoad();
  if (tab === 'musig2')   txm2Load();
  if (tab === 'dmusig2')  txdm2Load();
}

function txRefresh() {
  if (txState.tab === 'wallets')  txwLoad();
  if (txState.tab === 'musig2')   txm2Load();
  if (txState.tab === 'dmusig2')  txdm2Load();
}

// ── Cüzdanlar tab ─────────────────────────────────────────────────────────────

function _txExplorerBase(network) {
  return network === 'mainnet' ? 'https://mempool.space' : 'https://mempool.space/testnet4';
}

async function txwLoad() {
  // Cüzdan dropdown'unu doldur
  const sel = document.getElementById('txwWallet');
  if (sel) {
    const cur = sel.value;
    let opts = '<option value="">Tüm Cüzdanlar</option>';
    state.wallets.forEach(w => {
      opts += `<option value="${w.address}">${w.label}</option>`;
      const hdAddrs = w.hd_addresses || {};
      Object.entries(hdAddrs).forEach(([idx, info]) => {
        if (info.balance_sat > 0 || info.utxo_count > 0)
          opts += `<option value="${info.address}">${w.label} [${idx}]</option>`;
      });
    });
    sel.innerHTML = opts;
    if (cur) sel.value = cur;
  }

  const filterAddr = document.getElementById('txwWallet')?.value || '';
  _txwShowScanSince(filterAddr);
  await _txwUtxoPanel(filterAddr);

  const tbody = document.getElementById('txwBody');
  if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="empty">Yükleniyor…</td></tr>';

  // Hangi adresleri tarayacağız?
  // TX geçmişi — bakiyesi olan veya daha önce aktif olmuş adresler.
  // Hiç faaliyeti olmayan gap-limit adresleri taranmaz (gereksiz Esplora istekleri önlenir).
  let targets = [];
  if (!filterAddr) {
    state.wallets.forEach(w => {
      targets.push({ addr: w.address, label: w.label, network: w.network });
      const hdAddrs = w.hd_addresses || {};
      Object.entries(hdAddrs).forEach(([idx, info]) => {
        if (!info.address || info.address === w.address) return;
        // Aktif adres: bakiyesi > 0, UTXO'su var veya geçmişte balance çekilmiş (cached)
        const active = (info.balance_sat || 0) > 0
          || (info.utxo_count || 0) > 0
          || (state.balances[info.address]?.total_sat ?? -1) >= 0;
        if (active)
          targets.push({ addr: info.address, label: `${w.label} [${idx}]`, network: w.network });
      });
    });
  } else {
    for (const w of state.wallets) {
      if (w.address === filterAddr) { targets.push({ addr: filterAddr, label: w.label, network: w.network }); break; }
      const hdAddrs = w.hd_addresses || {};
      for (const [idx, info] of Object.entries(hdAddrs)) {
        if (info.address === filterAddr) { targets.push({ addr: filterAddr, label: `${w.label} [${idx}]`, network: w.network }); break; }
      }
    }
  }

  let allTxs = [];
  for (const t of targets) {
    try {
      const txs = await get(`/api/wallet/${t.addr}/txs`);
      state.txLastScan[t.addr] = new Date().toLocaleTimeString('tr-TR');
      allTxs = allTxs.concat(txs.map(tx => ({ ...tx, _label: t.label, _network: t.network, _addr: t.addr })));
    } catch {}
  }

  // Deduplicate
  const seen = new Set();
  allTxs = allTxs.filter(tx => { if (seen.has(tx.txid)) return false; seen.add(tx.txid); return true; });
  // Unconfirmed TX'ler en üste (block_time yoksa Infinity gibi davran)
  allTxs.sort((a, b) => {
    const ta = a.status?.confirmed ? (a.status?.block_time || 0) : Infinity;
    const tb = b.status?.confirmed ? (b.status?.block_time || 0) : Infinity;
    return tb - ta;
  });

  txState.wTxs = allTxs;
  txState.wPage = 1;

  // Son tarama
  const scanEl = document.getElementById('txwLastScan');
  if (scanEl) scanEl.textContent = allTxs.length ? `Son tarama: ${new Date().toLocaleTimeString('tr-TR')}` : '';

  txwRender();
}

function txwRender() {
  const search = (document.getElementById('txwSearch')?.value || '').toLowerCase().trim();
  let filtered = txState.wTxs;
  if (search) {
    filtered = filtered.filter(tx => {
      const hay = [tx.txid, tx._label, tx._addr].filter(Boolean).join(' ').toLowerCase();
      return hay.includes(search);
    });
  }
  txState.wFiltered = filtered;

  const countEl = document.getElementById('txwCount');
  if (countEl) countEl.textContent = filtered.length ? `${filtered.length} işlem` : '';

  const pages = Math.max(1, Math.ceil(filtered.length / txState.wPageSize));
  if (txState.wPage > pages) txState.wPage = 1;
  const start = (txState.wPage - 1) * txState.wPageSize;
  const page  = filtered.slice(start, start + txState.wPageSize);

  const tbody = document.getElementById('txwBody');
  if (!tbody) return;
  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">İşlem bulunamadı</td></tr>';
    document.getElementById('txwPager').innerHTML = '';
    return;
  }

  tbody.innerHTML = page.map(tx => {
    const date  = tx.status?.block_time ? new Date(tx.status.block_time * 1000).toLocaleString('tr-TR') : '—';
    const block = tx.status?.block_height ? `#${tx.status.block_height}` : 'Mempool';
    const outSum = tx.vout?.reduce((s, o) => s + o.value, 0) || 0;
    const base  = _txExplorerBase(tx._network);
    const txShort = tx.txid.substring(0, 16) + '…';
    return `<tr>
      <td><span class="bold" style="font-size:12px">${tx._label}</span></td>
      <td>
        <span class="mono-xs">${txShort}</span>
        <button class="btn btn-ghost sm" style="padding:2px 5px;font-size:10px" onclick="copyToClipboard('${tx.txid}');toast('TXID kopyalandı','success')">⎘</button>
      </td>
      <td class="muted" style="font-size:11px">${date}</td>
      <td><span class="muted">${block}</span></td>
      <td><span class="orange mono-sm">${outSum.toLocaleString()} sat</span></td>
      <td>${statusBadge(tx.status?.confirmed)}</td>
      <td><a class="link" href="${base}/tx/${tx.txid}" target="_blank">↗</a></td>
    </tr>`;
  }).join('');

  _txRenderPager('txwPager', pages, txState.wPage, p => { txState.wPage = p; txwRender(); });
}

function txwClearFilters() {
  const s = document.getElementById('txwSearch'); if (s) s.value = '';
  const w = document.getElementById('txwWallet'); if (w) w.value = '';
  txState.wPage = 1;
  txwLoad();
}

function _txwShowScanSince(filterAddr) {
  const row = document.getElementById('txwScanSinceRow');
  if (!row) return;
  const w = state.wallets.find(x => x.address === filterAddr ||
    Object.values(x.hd_addresses || {}).some(a => a.address === filterAddr));
  row.style.display = (w && w.hd_imported) ? 'flex' : 'none';
  if (w && w.hd_imported && w.scan_since) {
    const inp = document.getElementById('txwScanSinceInput');
    if (inp && !inp.value) inp.value = new Date(w.scan_since * 1000).toISOString().slice(0, 10);
  }
}

async function _txwUtxoPanel(filterAddr) {
  const panel = document.getElementById('txwUtxoPanel');
  if (!panel) return;
  if (!filterAddr) { panel.style.display = 'none'; return; }
  panel.style.display = '';
  panel.innerHTML = '<div style="color:var(--text-3);font-size:12px">UTXO yükleniyor…</div>';
  try {
    const utxos = await get(`/api/wallet/${filterAddr}/utxos`).catch(() => []);
    if (!Array.isArray(utxos) || !utxos.length) {
      panel.innerHTML = '<div style="color:var(--text-3);font-size:12px">Bu adrese ait UTXO yok.</div>';
      return;
    }
    const totalSat = utxos.reduce((s, u) => s + (u.value || 0), 0);
    panel.innerHTML = `
      <div style="font-size:12px;color:var(--text-2);margin-bottom:8px">
        <strong>${utxos.length} UTXO</strong> — Toplam: <strong style="color:var(--orange)">${totalSat.toLocaleString()} sat</strong>
        <button class="btn btn-ghost sm" style="margin-left:12px" onclick="quickSend('${filterAddr}')">Bu adresten gönder →</button>
      </div>
      <div class="table-wrap"><table class="data-table" style="font-size:12px">
        <thead><tr><th>TXID:vout</th><th>Tutar</th><th>Onay</th><th></th></tr></thead>
        <tbody>${utxos.map(u => {
          const id = `${u.txid}:${u.vout}`;
          return `<tr>
            <td><span class="mono-xs">${u.txid ? u.txid.substring(0,16) + '…:' + u.vout : '—'}</span></td>
            <td><span class="orange">${(u.value||0).toLocaleString()} sat</span></td>
            <td>${u.confirmations >= 1 ? `<span class="badge badge-green">${u.confirmations}</span>` : '<span class="badge badge-yellow">0</span>'}</td>
            <td><button class="btn btn-ghost sm" style="padding:2px 6px;font-size:10px" onclick="quickSend('${filterAddr}','${id}')">Gönder →</button></td>
          </tr>`;
        }).join('')}</tbody>
      </table></div>`;
  } catch {
    panel.innerHTML = '<div style="color:#f85149;font-size:12px">UTXO yüklenemedi.</div>';
  }
}

async function saveScanSince() {
  const filterAddr = document.getElementById('txwWallet')?.value;
  const dateVal    = document.getElementById('txwScanSinceInput')?.value;
  if (!filterAddr || !dateVal) return;
  const ts = Math.floor(new Date(dateVal).getTime() / 1000);
  const w  = state.wallets.find(x => x.address === filterAddr);
  if (!w) return;
  try {
    await post(`/api/wallet/${w.id}/scan-since`, { since: ts });
    toast('Tarama tarihi kaydedildi', 'success');
    txwLoad();
  } catch(e) { toast(e.message, 'error'); }
}

// ── MuSig2 tab ────────────────────────────────────────────────────────────────

async function txm2Load() {
  try {
    txState.m2Sessions = await get('/api/musig2/list');
  } catch { txState.m2Sessions = []; }
  txState.m2Page = 1;
  txm2Render();
}

function txm2Render() {
  const search = (document.getElementById('txm2Search')?.value || '').toLowerCase().trim();
  const status = document.getElementById('txm2Status')?.value || '';
  const DONE   = new Set(['BROADCAST', 'SIGNED']);

  let filtered = txState.m2Sessions.filter(s => {
    if (status === 'active' && DONE.has(s.state)) return false;
    if (status === 'done'   && !DONE.has(s.state)) return false;
    if (search) {
      const hay = [s.label, s.agg_address, s.id].filter(Boolean).join(' ').toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });
  txState.m2Filtered = filtered;

  const countEl = document.getElementById('txm2Count');
  if (countEl) countEl.textContent = filtered.length ? `${filtered.length} session` : '';

  const emptyEl = document.getElementById('txm2Empty');
  const tbody   = document.getElementById('txm2Body');
  if (!filtered.length) {
    if (tbody)   tbody.innerHTML = '';
    if (emptyEl) emptyEl.style.display = '';
    document.getElementById('txm2Pager').innerHTML = '';
    return;
  }
  if (emptyEl) emptyEl.style.display = 'none';

  const pages = Math.max(1, Math.ceil(filtered.length / txState.m2PageSize));
  if (txState.m2Page > pages) txState.m2Page = 1;
  const page = filtered.slice((txState.m2Page - 1) * txState.m2PageSize, txState.m2Page * txState.m2PageSize);
  const base = n => _txExplorerBase(n);

  tbody.innerHTML = page.map(s => {
    const isDone   = DONE.has(s.state);
    const badge    = isDone
      ? '<span class="badge badge-green">✓ Yayınlandı</span>'
      : `<span class="badge badge-yellow">${s.state}</span>`;
    const txShort  = s.txid ? s.txid.substring(0, 16) + '…' : '—';
    const txidAct  = s.txid
      ? `<button class="btn btn-ghost sm" style="padding:2px 5px;font-size:10px" onclick="copyToClipboard('${s.txid}');toast('TXID kopyalandı','success')">⎘</button>
         <a class="btn btn-ghost sm" style="padding:2px 5px;font-size:10px" href="${base(s.network)}/tx/${s.txid}" target="_blank">↗</a>` : '';
    const addrShort = s.to_address ? s.to_address.substring(0, 14) + '…' : '—';
    const amtStr    = s.amount_sat != null ? s.amount_sat.toLocaleString() + ' sat' : '—';
    return `<tr onclick="showTab('musig2')" style="cursor:pointer">
      <td><span class="bold">${s.label}</span></td>
      <td><span class="mono-xs" style="color:var(--text-3)">${s.network}</span></td>
      <td><span class="mono-xs">${txShort}</span>${txidAct}</td>
      <td><span class="orange bold">${amtStr}</span></td>
      <td><span class="mono-xs" title="${s.to_address||''}">${addrShort}</span></td>
      <td>${badge}</td>
      <td><button class="btn btn-ghost sm" onclick="event.stopPropagation();showTab('musig2')">Aç →</button></td>
    </tr>`;
  }).join('');

  _txRenderPager('txm2Pager', pages, txState.m2Page, p => { txState.m2Page = p; txm2Render(); });
}

function txm2ClearFilters() {
  const s = document.getElementById('txm2Search'); if (s) s.value = '';
  const st = document.getElementById('txm2Status'); if (st) st.value = '';
  txState.m2Page = 1; txm2Render();
}

// ── Dağıtık MuSig2 tab ────────────────────────────────────────────────────────

async function txdm2Load() {
  try {
    txState.dm2Sessions = await get('/api/musig2d/list');
    // state.dmusig2SessionsAll'ı da güncelle (tutarlılık için)
    state.dmusig2SessionsAll = txState.dm2Sessions;
    state.dmusig2Sessions = txState.dm2Sessions.filter(s => s.state !== 'BROADCAST');
  } catch { txState.dm2Sessions = []; }
  txState.dm2Page = 1;
  txdm2Render();
}

function txdm2Render() {
  const search = (document.getElementById('txdm2Search')?.value || '').toLowerCase().trim();
  const net    = document.getElementById('txdm2Net')?.value || '';
  const minAmt = parseInt(document.getElementById('txdm2Min')?.value) || 0;
  const maxAmt = parseInt(document.getElementById('txdm2Max')?.value) || Infinity;
  const status = document.getElementById('txdm2Status')?.value || '';
  const DONE   = new Set(['BROADCAST']);

  let filtered = txState.dm2Sessions.filter(s => {
    if (net && s.network !== net) return false;
    if (status === 'active' && DONE.has(s.state)) return false;
    if (status === 'done'   && !DONE.has(s.state)) return false;
    if (minAmt && (s.amount_sat || 0) < minAmt) return false;
    if (maxAmt !== Infinity && (s.amount_sat || 0) > maxAmt) return false;
    if (search) {
      const hay = [s.label, s.txid, s.to_address, s.id, s.agg_address].filter(Boolean).join(' ').toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });
  txState.dm2Filtered = filtered;

  const countEl = document.getElementById('txdm2Count');
  if (countEl) countEl.textContent = filtered.length ? `${filtered.length} session` : '';

  const emptyEl = document.getElementById('txdm2Empty');
  const tbody   = document.getElementById('txdm2Body');
  if (!filtered.length) {
    if (tbody)   tbody.innerHTML = '';
    if (emptyEl) emptyEl.style.display = '';
    document.getElementById('txdm2Pager').innerHTML = '';
    return;
  }
  if (emptyEl) emptyEl.style.display = 'none';

  const pages = Math.max(1, Math.ceil(filtered.length / txState.dm2PageSize));
  if (txState.dm2Page > pages) txState.dm2Page = 1;
  const page = filtered.slice((txState.dm2Page - 1) * txState.dm2PageSize, txState.dm2Page * txState.dm2PageSize);

  tbody.innerHTML = page.map(s => {
    const DONE_SET = new Set(['BROADCAST', 'SIGNED']);
    const badge    = s.state === 'BROADCAST'
      ? '<span class="badge badge-green">✓ Yayınlandı</span>'
      : DONE_SET.has(s.state)
      ? '<span class="badge badge-yellow">İmzalandı</span>'
      : dStateBadge(s.state);
    const dateStr   = s.created_at ? new Date(s.created_at * 1000).toLocaleString('tr-TR') : '—';
    const txShort   = s.txid ? s.txid.substring(0, 16) + '…' : '—';
    const base      = _txExplorerBase(s.network);
    const txidAct   = s.txid
      ? `<button class="btn btn-ghost sm" style="padding:2px 5px;font-size:10px" onclick="event.stopPropagation();copyToClipboard('${s.txid}');toast('TXID kopyalandı','success')">⎘</button>
         <a class="btn btn-ghost sm" style="padding:2px 5px;font-size:10px" href="${base}/tx/${s.txid}" target="_blank" onclick="event.stopPropagation()">↗</a>` : '';
    const addrShort = s.to_address ? s.to_address.substring(0, 14) + '…' : '—';
    const amtStr    = s.amount_sat != null ? s.amount_sat.toLocaleString() + ' sat' : '—';
    const feeRow    = s.fee_sat ? `<div class="mono-xs" style="color:var(--text-3)">fee: ${s.fee_sat.toLocaleString()} sat</div>` : '';
    return `<tr onclick="dOpenSession('${s.id}')" style="cursor:pointer">
      <td><span class="bold">${s.label}</span></td>
      <td><span class="mono-xs" style="color:var(--text-3)">${s.network}</span></td>
      <td><span class="mono-xs">${txShort}</span>${txidAct}</td>
      <td style="font-size:12px;color:var(--text-3)">${dateStr}</td>
      <td><span class="orange bold">${amtStr}</span>${feeRow}</td>
      <td class="d-hide-mobile"><span class="mono-xs" title="${s.to_address||''}">${addrShort}</span></td>
      <td>${badge}</td>
      <td><button class="btn btn-ghost sm" onclick="event.stopPropagation();dOpenSession('${s.id}')">Aç →</button></td>
    </tr>`;
  }).join('');

  _txRenderPager('txdm2Pager', pages, txState.dm2Page, p => { txState.dm2Page = p; txdm2Render(); });
}

function txdm2ClearFilters() {
  ['txdm2Search','txdm2Min','txdm2Max'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  const net = document.getElementById('txdm2Net'); if (net) net.value = '';
  const st  = document.getElementById('txdm2Status'); if (st) st.value = '';
  txState.dm2Page = 1; txdm2Render();
}

// Ortak pager
function _txRenderPager(id, pages, current, onPage) {
  const el = document.getElementById(id);
  if (!el) return;
  if (pages <= 1) { el.innerHTML = ''; return; }
  el.innerHTML =
    `<button class="btn btn-ghost sm" ${current === 1 ? 'disabled style="opacity:.4"' : ''} onclick="(${onPage})(${current - 1})">← Önceki</button>` +
    `<span style="font-size:12px;color:var(--text-3);padding:0 10px">${current} / ${pages}</span>` +
    `<button class="btn btn-ghost sm" ${current === pages ? 'disabled style="opacity:.4"' : ''} onclick="(${onPage})(${current + 1})">Sonraki →</button>`;
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
          <button class="btn btn-ghost sm" style="color:#e05252;margin-left:auto" onclick="deleteMusig2Session('${s.id}')" title="Oturumu sil">✕ Sil</button>
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

async function deleteMusig2Session(sid) {
  if (!confirm('Bu MuSig2 oturumu silinsin mi? Henüz harcanmamış UTXO varsa adresi not alın.')) return;
  try {
    await fetch(`/api/musig2/${sid}`, { method: 'DELETE' });
    toast('Oturum silindi', 'success');
    await loadMusig2();
  } catch (e) {
    toast(e.message, 'error');
  }
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
  tbody.innerHTML = state.wallets.map(w => {
    const isHD = w.hd;
    const scanBtn = isHD
      ? `<button class="btn btn-ghost sm" onclick="hdScanWallet('${w.id}')" title="HD adreslerini tara">⟳ HD Tara</button>`
      : '';
    // 1B: panel daha önce açıksa açık gelsin
    const panelOpen = !!state.hdPanelOpen[`wallets-${w.id}`];
    return `
    <tr id="wallet-row-${w.id}">
      <td><span class="bold">${w.label}</span>${w.hd_imported ? ' <span class="badge badge-yellow" style="font-size:9px">import</span>' : ''}</td>
      <td><span class="badge ${w.network === 'mainnet' ? 'badge-green' : 'badge-yellow'}">${w.network}</span></td>
      <td>
        <span class="mono-sm truncate" style="max-width:260px;display:inline-block">${w.address}</span>
        <button class="btn btn-ghost sm" onclick="copyAddr('${w.address}')" style="margin-left:4px">⎘</button>
      </td>
      <td><span class="mono-xs truncate" style="max-width:200px;display:inline-block">${w.xonly_pk}</span></td>
      <td style="white-space:nowrap">
        <button class="btn btn-ghost sm" onclick="quickReceive('${w.address}')">Al</button>
        ${scanBtn}
        <button class="btn btn-ghost sm" onclick="downloadBSMS('${w.label}')" title="Sparrow Wallet için Output Descriptor indir">⬇ Sparrow</button>
        <button class="btn btn-danger sm" onclick="removeWallet('${w.address}')">Sil</button>
      </td>
    </tr>
    <tr id="wallet-hd-panel-${w.id}" style="display:${panelOpen ? '' : 'none'}">
      <td colspan="5" style="padding:0">
        <div id="wallet-hd-body-${w.id}" style="padding:12px 16px;background:var(--bg-2);border-top:1px solid var(--border)">
          ${panelOpen && state.hdScanResults[w.id] ? _renderHDBody(w.id) : ''}
        </div>
      </td>
    </tr>`;
  }).join('');
}

function _renderHDBody(walletId) {
  const res = state.hdScanResults[walletId];
  if (!res) return '';
  const addrs = res.addresses.filter(a => a.balance_sat > 0 || a.utxo_count > 0);
  if (!addrs.length) return '<div style="color:var(--text-3);font-size:13px;padding:8px 0">Bakiye bulunan adres yok.</div>';
  return `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
      <span style="font-size:13px;color:var(--text-2)">${res.scanned} adres tarandı &mdash;
        <strong style="color:var(--orange)">${addrs.length} aktif</strong>,
        toplam <strong>${(res.total_balance_sat / 1e8).toFixed(8)} BTC</strong></span>
      <button class="btn btn-ghost sm" onclick="hdScanWallet('${walletId}')">↺ Yenile</button>
    </div>
    <div class="table-wrap">
      <table class="data-table" style="font-size:12px">
        <thead><tr><th>İndis</th><th>Adres</th><th>Bakiye</th><th>UTXO</th><th></th></tr></thead>
        <tbody>
          ${addrs.map(a => `
            <tr>
              <td style="color:var(--text-3)">${a.index ?? '?'}</td>
              <td><span class="mono-xs" style="word-break:break-all">${a.address}</span></td>
              <td><strong style="color:var(--orange)">${(a.balance_sat / 1e8).toFixed(8)} BTC</strong></td>
              <td>${a.utxo_count}</td>
              <td>
                <button class="btn btn-ghost sm" onclick="copyText('${a.address}')">⎘</button>
                <button class="btn btn-ghost sm" onclick="quickSend('${a.address}')">Gönder</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}

// ── HD Cüzdan Tarama ──────────────────────────────────────────────────────────

async function hdScanWallet(walletId) {
  const panel  = document.getElementById(`wallet-hd-panel-${walletId}`);
  const body   = document.getElementById(`wallet-hd-body-${walletId}`);
  const scanBtn = document.querySelector(`button[onclick="hdScanWallet('${walletId}')"]`);
  const key    = `wallets-${walletId}`;

  // Toggle: zaten açık ve veri varsa kapat
  if (panel && panel.style.display !== 'none' && state.hdScanResults[walletId]) {
    const isCalled = arguments[1] === 'refresh';
    if (!isCalled) {
      state.hdPanelOpen[key] = false;
      panel.style.display = 'none';
      return;
    }
  }

  // Loading state
  state.hdPanelOpen[key] = true;
  if (panel) panel.style.display = '';
  if (body)  body.innerHTML = '<div style="color:var(--text-3);font-size:13px;padding:8px 0">⏳ Adresler taranıyor… lütfen bekleyin.</div>';
  if (scanBtn) { scanBtn.disabled = true; scanBtn.textContent = '⏳ Taranıyor…'; }

  try {
    const res = await post(`/api/wallet/${walletId}/hd-scan`, {});
    state.hdScanResults[walletId] = res;
    if (body) body.innerHTML = _renderHDBody(walletId);
    populateWalletSelects();
    _renderDashboardAddressTable();
  } catch(e) {
    if (body) body.innerHTML = `<div style="color:#f85149;font-size:13px">Tarama hatası: ${e.message}</div>`;
  } finally {
    // scanBtn artık _renderHDBody içindeki "Yenile" butonu — satır yeniden render edildi,
    // dıştaki buton ise DOM'da kalmış olabilir, onu da sıfırla
    const btn = document.querySelector(`button[onclick="hdScanWallet('${walletId}')"]`);
    if (btn) { btn.disabled = false; btn.textContent = '⟳ HD Tara'; }
  }
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => toast('Kopyalandı', 'success')).catch(() => {});
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
  // addrWalletSelect — Adresler sekmesi için
  const addrEl = document.getElementById('addrWalletSelect');
  if (addrEl) {
    const cur = addrEl.value;
    let opts = '<option value="">— Cüzdan seçin —</option>';
    state.wallets.forEach(w => {
      opts += `<option value="${w.id}">${w.label}</option>`;
    });
    addrEl.innerHTML = opts;
    if (cur) addrEl.value = cur;
  }

  // receiveWalletSelect — wallet ID kullanır; fresh address async olarak çekilir
  const recvEl = document.getElementById('receiveWalletSelect');
  if (recvEl) {
    const cur = recvEl.value;
    let opts = '<option value="">— Cüzdan seçin —</option>';
    state.wallets.forEach(w => {
      opts += `<option value="${w.id}">${w.label}${w.hd ? '' : ` — ${w.address.substring(0, 14)}…`}</option>`;
    });
    recvEl.innerHTML = opts;
    if (cur) recvEl.value = cur;
  }

  // sendFromSelect — HD alt-adresler de dahil, cüzdan başına optgroup
  const sendEl = document.getElementById('sendFromSelect');
  if (sendEl) {
    const cur = sendEl.value;
    let opts = '<option value="">— Cüzdan seçin —</option>';
    state.wallets.forEach(w => {
      const hdAddrs = w.hd_addresses || {};
      const activeHD = Object.entries(hdAddrs).filter(
        ([, info]) => info.balance_sat > 0 || info.utxo_count > 0
      );
      if (activeHD.length) {
        // HD cüzdan: optgroup ile birincil + alt-adresler
        opts += `<optgroup label="── ${w.label}">`;
        opts += `<option value="${w.address}">${w.label} (birincil) — ${w.address.substring(0, 16)}…</option>`;
        activeHD.forEach(([idx, info]) => {
          opts += `<option value="${info.address}">${w.label} [${idx}] — ${info.address.substring(0, 16)}… (${(info.balance_sat/1e8).toFixed(4)} BTC)</option>`;
        });
        opts += '</optgroup>';
      } else {
        opts += `<option value="${w.address}">${w.label} — ${w.address.substring(0, 16)}…</option>`;
      }
    });
    sendEl.innerHTML = opts;
    if (cur) sendEl.value = cur;
  }
}

function quickReceive(address) {
  // address bir BTC adresi — wallet ID'ye çevir
  const w = state.wallets.find(x => x.address === address ||
    Object.values(x.hd_addresses || {}).some(a => a.address === address));
  const id = w ? w.id : address;
  document.getElementById('receiveWalletSelect').value = id;
  showTab('receive');
  updateReceive();
}

function quickSend(address, utxoId) {
  document.getElementById('sendFromSelect').value = address;
  if (utxoId) sendState.preSelect = utxoId;
  showTab('send');
  updateSendBalance();
}

function copyToClipboard(text) {
  // Secure context (HTTPS / localhost) → modern clipboard API
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).catch(() => _clipboardFallback(text));
  }
  // HTTP / non-secure → execCommand fallback
  return Promise.resolve(_clipboardFallback(text));
}

function _clipboardFallback(text) {
  try {
    const inp = document.createElement('textarea');
    inp.value = text;
    inp.style.cssText = 'position:fixed;opacity:0;top:0;left:0;width:1px;height:1px';
    document.body.appendChild(inp);
    inp.focus();
    inp.select();
    inp.setSelectionRange(0, text.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(inp);
    return ok;
  } catch {
    return false;
  }
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

// ── Cüzdan Import ──────────────────────────────────────────────────────────

function openImportWalletModal() {
  document.getElementById('importWalletLabel').value    = '';
  document.getElementById('importWalletTprv').value     = '';
  document.getElementById('importWalletFile').value     = '';
  document.getElementById('importWalletFileStatus').textContent = '';
  document.getElementById('importWalletPreview').style.display = 'none';
  openModal('importWalletModal');
}

function importWalletParseFile(input) {
  const file = input.files[0];
  if (!file) return;
  const statusEl = document.getElementById('importWalletFileStatus');
  statusEl.textContent = 'Okunuyor…';

  const reader = new FileReader();
  reader.onload = e => {
    const text = e.target.result;
    // MASTER_TPRV veya MASTER_XPRV satırını bul
    const match = text.match(/(?:MASTER_TPRV|MASTER_XPRV)\s*(?:\(.*?\))?:\s*\n?([a-zA-Z0-9]+)/);
    if (!match) {
      statusEl.textContent = 'MASTER_TPRV satırı bulunamadı.';
      statusEl.style.color = '#f85149';
      return;
    }
    document.getElementById('importWalletTprv').value = match[1].trim();

    // Etiketi dosya adından al (uzantısız)
    const labelEl = document.getElementById('importWalletLabel');
    if (!labelEl.value) {
      labelEl.value = file.name.replace(/\.(txt|descriptor)$/i, '');
    }

    // Ağı tahmin et: "mainnet" geçiyorsa mainnet, yoksa testnet4
    const net = text.includes('mainnet') ? 'mainnet' : 'testnet4';
    document.getElementById('importWalletNetwork').value = net;

    statusEl.textContent = 'Anahtar bulundu.';
    statusEl.style.color = '#3fb950';
  };
  reader.readAsText(file);
}

async function importWalletPreview() {
  const tprv = document.getElementById('importWalletTprv').value.trim();
  const net  = document.getElementById('importWalletNetwork').value;
  if (!tprv) { toast('MASTER_TPRV girin', 'error'); return; }
  try {
    // Backend'e preview isteği gönder (label geçici)
    const res = await post('/api/wallet/import', {
      label: '__preview__',
      network: net,
      master_xprv: tprv,
    });
    // Preview sonrası cüzdan kaydedildi — sil
    await fetch(`/api/wallet/${res.id}`, { method: 'DELETE' });
    document.getElementById('importWalletAddr').textContent = res.address;
    document.getElementById('importWalletPreview').style.display = '';
  } catch(e) {
    toast(`Doğrulama hatası: ${e.message}`, 'error');
  }
}

async function importWallet() {
  const label = document.getElementById('importWalletLabel').value.trim();
  const net   = document.getElementById('importWalletNetwork').value;
  const tprv  = document.getElementById('importWalletTprv').value.trim();
  if (!label) { toast('Etiket girin', 'error'); return; }
  if (!tprv)  { toast('MASTER_TPRV girin', 'error'); return; }
  try {
    await post('/api/wallet/import', { label, network: net, master_xprv: tprv });
    closeModal('importWalletModal');
    toast('Cüzdan import edildi', 'success');
    loadWallets();
  } catch(e) {
    toast(`Import hatası: ${e.message}`, 'error');
  }
}

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
        ${isActive
          ? `<div style="color:#f0883e;font-size:0.85em;cursor:pointer;text-decoration:underline" onclick="dOpenSession('${entry.session_id}')">${actionLabel}</div>`
          : `<div style="color:#8b949e;font-size:0.85em">${actionLabel}</div>`
        }
        ${participantRow}
        ${expiryRow}
        ${entry.source_session_id
          ? `<div style="font-size:10px;color:var(--blue);margin-top:2px">↩ Para üstü işlemi</div>`
          : ''}
      </div>
      <button class="btn btn-ghost sm" onclick="dOpenSession('${entry.session_id}')" style="white-space:nowrap;align-self:center">Aç →</button>
    </div>`;
}

function dRenderDashboard(actions) {
  const dash = document.getElementById('dmusig2Dashboard');
  if (!dash) return;

  // Arşiv sekmesi aktifken dashboard'u gösterme — polling bunu ezmemeli
  if (state.dmusig2SubTab !== 'active') return;

  // Dashboard yalnızca pubkey biliniyorsa anlamlı
  if (!state.myPubkey || !actions || !actions.length) {
    dash.style.display = 'none';
    return;
  }
  dash.style.display = '';

  const ACTIVE  = new Set(['build_tx','submit_nonce','submit_partial_sig','broadcast']);
  const WAITING = new Set(['wait_pubkeys','wait_coordinator','wait_nonce','wait_sig']);

  // BROADCAST (done) session'lar arşive ait — aktif tab'da gösterme
  const pending  = actions.filter(a => ACTIVE.has(a.action));
  const watching = actions.filter(a => WAITING.has(a.action));

  const pendingEl  = document.getElementById('dmusig2DashPending');
  const watchingEl = document.getElementById('dmusig2DashWatching');

  pendingEl.innerHTML = pending.length
    ? pending.map(_dDashCard).join('')
    : '<div class="empty-state" style="padding:10px 16px;font-size:0.85em">Bekleyen aksiyon yok.</div>';

  watchingEl.innerHTML = watching.length
    ? watching.map(_dDashCard).join('')
    : '<div class="empty-state" style="padding:10px 16px;font-size:0.85em">İzlenen oturum yok.</div>';
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

// ── Arşiv ──────────────────────────────────────────────────────────────────

function dShowSubTab(tab) {
  const isActive = tab === 'active';
  state.dmusig2SubTab = tab;

  const dashboard = document.getElementById('dmusig2Dashboard');
  const list      = document.getElementById('dmusig2SessionList');
  const archive   = document.getElementById('dmusig2Archive');
  const detail    = document.getElementById('dmusig2Detail');

  if (dashboard) dashboard.style.display = isActive ? '' : 'none';
  if (list)      list.style.display      = isActive ? '' : 'none';
  if (archive)   archive.style.display   = isActive ? 'none' : '';
  if (detail && detail.style.display !== 'none') detail.style.display = 'none';

  const btnA = document.getElementById('dSubTabActive');
  const btnB = document.getElementById('dSubTabArchive');
  if (btnA) {
    btnA.style.borderBottomColor = isActive ? 'var(--orange)' : 'transparent';
    btnA.style.color             = isActive ? 'var(--orange)' : 'var(--text-2)';
  }
  if (btnB) {
    btnB.style.borderBottomColor = !isActive ? 'var(--orange)' : 'transparent';
    btnB.style.color             = !isActive ? 'var(--orange)' : 'var(--text-2)';
  }

  if (!isActive) {
    // Arşiv'e geçildiğinde her zaman taze veri yükle
    dArchiveLoad();
  }
}

async function dArchiveLoad() {
  try {
    const all = await get('/api/musig2d/list');
    state.archiveSessions = all
      .filter(s => s.state === 'BROADCAST')
      .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    const badge = document.getElementById('dArchiveBadge');
    if (badge) badge.textContent = state.archiveSessions.length || '';
    state.archivePage = 1;
    dArchiveRender();
  } catch(e) {
    uiLog('Arşiv yüklenemedi: ' + e.message, 'ERR');
    toast('Arşiv yüklenemedi', 'error');
  }
}

function dArchiveRender() {
  const search = (document.getElementById('dArchiveSearch')?.value || '').toLowerCase().trim();
  const net    = document.getElementById('dArchiveNet')?.value || '';
  const minAmt = parseInt(document.getElementById('dArchiveMinAmt')?.value) || 0;
  const maxAmt = parseInt(document.getElementById('dArchiveMaxAmt')?.value) || Infinity;

  state.archiveFiltered = state.archiveSessions.filter(s => {
    if (net && s.network !== net) return false;
    if (minAmt && (s.amount_sat || 0) < minAmt) return false;
    if (maxAmt !== Infinity && (s.amount_sat || 0) > maxAmt) return false;
    if (search) {
      const hay = [s.label, s.txid, s.to_address, s.id].filter(Boolean).join(' ').toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });

  _dArchiveUpdateChips({ search, net, minAmt, maxAmt });

  const countEl = document.getElementById('dArchiveCount');
  if (countEl) countEl.textContent = state.archiveFiltered.length ? state.archiveFiltered.length + ' session' : '';

  const total  = state.archiveFiltered.length;
  const pages  = Math.max(1, Math.ceil(total / state.archivePageSize));
  if (state.archivePage > pages) state.archivePage = 1;
  const start    = (state.archivePage - 1) * state.archivePageSize;
  const pageData = state.archiveFiltered.slice(start, start + state.archivePageSize);

  const emptyEl = document.getElementById('dArchiveEmpty');
  const tableEl = document.getElementById('dArchiveTable');

  if (!total) {
    if (emptyEl) emptyEl.style.display = '';
    if (tableEl) tableEl.style.display = 'none';
    document.getElementById('dArchivePager').innerHTML = '';
    return;
  }

  if (emptyEl) emptyEl.style.display = 'none';
  if (tableEl) tableEl.style.display = '';

  const explorerBase = n => n === 'mainnet'
    ? 'https://mempool.space/tx/'
    : 'https://mempool.space/testnet4/tx/';

  document.getElementById('dArchiveBody').innerHTML = pageData.map(s => {
    const dateStr   = s.created_at ? new Date(s.created_at * 1000).toLocaleString('tr-TR') : '—';
    const txShort   = s.txid ? s.txid.substring(0, 16) + '…' : '—';
    const amtStr    = s.amount_sat != null ? s.amount_sat.toLocaleString() + ' sat' : '—';
    const addrShort = s.to_address ? s.to_address.substring(0, 14) + '…' : '—';
    const netColor  = s.network === 'mainnet' ? 'var(--red)' : 'var(--text-3)';
    const badge     = s.state === 'BROADCAST'
      ? '<span class="badge badge-green">✓ Yayınlandı</span>'
      : '<span class="badge badge-yellow">İmzalandı</span>';
    const txidActions = s.txid
      ? `<button class="btn btn-ghost sm" style="margin-left:4px;padding:2px 6px;font-size:11px"
           onclick="event.stopPropagation();copyToClipboard('${s.txid}');toast('TXID kopyalandı','success')"
           title="TXID kopyala">⎘</button>
         <a class="btn btn-ghost sm" style="margin-left:2px;padding:2px 6px;font-size:11px"
           href="${explorerBase(s.network)}${s.txid}" target="_blank"
           onclick="event.stopPropagation()">↗</a>`
      : '';
    const feeRow = s.fee_sat ? `<div class="mono-xs" style="color:var(--text-3)">fee: ${s.fee_sat.toLocaleString()} sat</div>` : '';

    return `
      <tr onclick="dArchiveOpenDetail('${s.id}')">
        <td><span class="bold">${s.label}</span></td>
        <td><span class="mono-xs" style="color:${netColor}">${s.network}</span></td>
        <td><span class="mono-xs">${txShort}</span>${txidActions}</td>
        <td style="font-size:12px;color:var(--text-3);white-space:nowrap">${dateStr}</td>
        <td><span class="orange bold">${amtStr}</span>${feeRow}</td>
        <td class="d-hide-mobile">
          <span class="mono-xs" style="max-width:120px;display:inline-block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
            title="${s.to_address || ''}">${addrShort}</span>
        </td>
        <td>${badge}</td>
        <td>
          <button class="btn btn-ghost sm" style="white-space:nowrap"
            onclick="event.stopPropagation();dArchiveOpenDetail('${s.id}')">Aç →</button>
        </td>
      </tr>`;
  }).join('');

  _dArchiveRenderPager(pages);
}

function _dArchiveRenderPager(pages) {
  const pager = document.getElementById('dArchivePager');
  if (!pager) return;
  if (pages <= 1) { pager.innerHTML = ''; return; }
  const p = state.archivePage;
  pager.innerHTML =
    `<button class="btn btn-ghost sm" ${p === 1 ? 'disabled style="opacity:.4"' : ''}
       onclick="state.archivePage=${p-1};dArchiveRender()">← Önceki</button>` +
    `<span style="font-size:12px;color:var(--text-3);padding:0 10px">${p} / ${pages}</span>` +
    `<button class="btn btn-ghost sm" ${p === pages ? 'disabled style="opacity:.4"' : ''}
       onclick="state.archivePage=${p+1};dArchiveRender()">Sonraki →</button>`;
}

function _dArchiveUpdateChips({ search, net, minAmt, maxAmt }) {
  const chips = [];
  if (search) chips.push({ label: '🔍 ' + search, clear: "document.getElementById('dArchiveSearch').value='';dArchiveRender()" });
  if (net)    chips.push({ label: 'Ağ: ' + net,   clear: "document.getElementById('dArchiveNet').value='';dArchiveRender()" });
  if (minAmt) chips.push({ label: '≥ ' + minAmt.toLocaleString() + ' sat', clear: "document.getElementById('dArchiveMinAmt').value='';dArchiveRender()" });
  if (maxAmt !== Infinity && maxAmt) chips.push({ label: '≤ ' + maxAmt.toLocaleString() + ' sat', clear: "document.getElementById('dArchiveMaxAmt').value='';dArchiveRender()" });

  const container = document.getElementById('dArchiveChips');
  if (!container) return;
  if (!chips.length) { container.style.display = 'none'; return; }
  container.style.display = 'flex';
  container.innerHTML = chips.map(c =>
    `<span class="filter-chip">${c.label}
       <button class="filter-chip-remove" onclick="${c.clear}" title="Kaldır">×</button>
     </span>`
  ).join('') +
  `<button class="btn btn-ghost sm" onclick="dArchiveClearFilters()" style="font-size:11px;padding:2px 8px">Tümünü Temizle</button>`;
}

function dArchiveClearFilters() {
  ['dArchiveSearch', 'dArchiveMinAmt', 'dArchiveMaxAmt'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  const net = document.getElementById('dArchiveNet');
  if (net) net.value = '';
  state.archivePage = 1;
  dArchiveRender();
}

function dArchiveOpenDetail(sid) {
  state._archiveOpenedDetail = true;
  showTab('musig2d');
  dOpenSession(sid);
}

// ── Para Üstü Hızlı Session ────────────────────────────────────────────────

async function dCheckChangeBalance(s) {
  const card    = document.getElementById('dmusig2ChangeCard');
  const actions = document.getElementById('dmusig2ChangeActions');
  if (!card || !s.agg_address) { if (card) card.style.display = 'none'; return; }
  try {
    const bal = await get(`/api/wallet/${s.agg_address}/balance`);
    if (!bal.confirmed_sat || bal.confirmed_sat <= 0) {
      card.style.display = 'none';
      return;
    }
    card.style.display = 'flex';
    document.getElementById('dmusig2ChangeAmount').textContent =
      bal.confirmed_sat.toLocaleString() + ' sat onaylı';
    document.getElementById('dmusig2ChangeAddr').textContent = s.agg_address;

    if (actions) {
      if (state.myRole === 'coordinator') {
        actions.innerHTML = `<button class="btn btn-primary" onclick="dOpenQuickSessionModal()">⚡ Yeni İşlem Başlat →</button>`;
      } else {
        actions.innerHTML = `<span style="font-size:12px;color:var(--text-3)">⌛ Koordinatör yeni işlemi başlatacak</span>`;
      }
    }
    state._changeBalance = bal.confirmed_sat;
  } catch(_) {
    if (card) card.style.display = 'none';
  }
}

function dOpenQuickSessionModal() {
  const s = state.dmusig2Session;
  if (!s) return;

  const pEl = document.getElementById('qsParticipants');
  if (pEl) {
    pEl.innerHTML = s.participants.map(p => `
      <div style="display:flex;align-items:center;gap:10px;padding:4px 0;border-bottom:1px solid var(--border)">
        <span style="min-width:90px;font-size:12px;color:var(--text-2)">${p.label}</span>
        <span class="mono-xs" style="color:var(--text-3)">${p.pubkey ? p.pubkey.substring(0,20) + '…' : '—'}</span>
        <span style="font-size:10px;color:var(--text-3);margin-left:auto">🔒 kilitli</span>
      </div>`).join('');
  }

  const txidEl    = document.getElementById('qsSourceTxid');
  const explorerEl = document.getElementById('qsSourceExplorer');
  if (txidEl) txidEl.textContent = s.txid ? s.txid.substring(0, 24) + '…' : s.id;
  if (explorerEl && s.txid) {
    const base = s.network === 'mainnet' ? 'https://mempool.space/tx/' : 'https://mempool.space/testnet4/tx/';
    explorerEl.href = base + s.txid;
  }

  const balEl = document.getElementById('qsAvailableBalance');
  if (balEl) balEl.textContent = (state._changeBalance || 0).toLocaleString() + ' sat';

  const labelEl = document.getElementById('qsLabel');
  if (labelEl) labelEl.value = s.label + ' Devam';

  ['qsDescription','qsToAddr','qsAmount'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });

  openModal('quickSessionModal');
}

function dQuickSessionFillMax() {
  const bal = state._changeBalance || 0;
  const fee = parseInt(document.getElementById('qsFee')?.value) || 500;
  const max = bal - fee;
  if (max > 546) {
    document.getElementById('qsAmount').value = max;
  } else {
    toast('Bakiye yetersiz', 'error');
  }
}

async function dQuickSessionCreate() {
  const s = state.dmusig2Session;
  if (!s) return;

  const toAddr = document.getElementById('qsToAddr')?.value.trim();
  const amount = parseInt(document.getElementById('qsAmount')?.value);
  const fee    = parseInt(document.getElementById('qsFee')?.value) || 500;
  const desc   = document.getElementById('qsDescription')?.value.trim() || '';
  const label  = document.getElementById('qsLabel')?.value.trim() || s.label + ' Devam';

  if (!toAddr) { toast('Hedef adres girin', 'error'); return; }
  if (!amount || amount < 546) { toast('Geçerli tutar girin (min 546 sat)', 'error'); return; }

  try {
    const newSession = await post('/api/musig2d/new', {
      label,
      n_participants: s.n,
      network: s.network,
      source_session_id: s.id,
    });

    const myIdx = state.myIndex ?? 0;
    const mySk  = localStorage.getItem(D_SK_KEY(s.id));
    if (!mySk) { toast('SK bulunamadı — oturumu yeniden açın', 'error'); return; }
    const myPk  = MuSig2D.derivePublicKey(mySk);

    await post(`/api/musig2d/${newSession.id}/register`, {
      participant_index: myIdx,
      pubkey_hex: myPk,
    });

    for (const p of s.participants) {
      if (p.index === myIdx || !p.pubkey) continue;
      await post(`/api/musig2d/${newSession.id}/register`, {
        participant_index: p.index,
        pubkey_hex: p.pubkey,
      });
    }

    localStorage.setItem(D_SK_KEY(newSession.id), mySk);
    localStorage.setItem(D_IDX_KEY(newSession.id), String(myIdx));

    closeModal('quickSessionModal');
    toast('Yeni session oluşturuldu — TX aşamasına hazır', 'success');

    await dOpenSession(newSession.id);

    if (state.myRole === 'coordinator') {
      const toEl  = document.getElementById('dmusig2TxTo');
      const amtEl = document.getElementById('dmusig2TxAmount');
      const feeEl = document.getElementById('dmusig2TxFee');
      const dscEl = document.getElementById('dmusig2TxDesc');
      if (toEl)  toEl.value  = toAddr;
      if (amtEl) amtEl.value = amount;
      if (feeEl) feeEl.value = fee;
      if (dscEl) dscEl.value = desc;
      document.getElementById('dmusig2TxCard')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  } catch(e) {
    toast(`Hata: ${e.message}`, 'error');
  }
}

function dExport(type) {
  document.getElementById('dExportMenu').style.display = 'none';
  const CSV_COLS = ['id','label','network','state','txid','amount_sat','fee_sat','to_address','agg_xonly','description','created_at'];
  const source = type === 'json-all' ? state.archiveSessions : state.archiveFiltered;
  if (!source.length) { toast('Dışa aktarılacak kayıt yok', 'error'); return; }

  const escape = v => {
    const str = v == null ? '' : String(v);
    return (str.includes(',') || str.includes('"') || str.includes('\n'))
      ? '"' + str.replace(/"/g, '""') + '"' : str;
  };

  let blob, filename;
  if (type === 'csv') {
    const rows = [CSV_COLS.join(','), ...source.map(s => CSV_COLS.map(k => escape(s[k])).join(','))];
    blob = new Blob([rows.join('\n')], { type: 'text/csv;charset=utf-8' });
    filename = 'musig2-archive-' + new Date().toISOString().slice(0, 10) + '.csv';
  } else {
    blob = new Blob([JSON.stringify(source, null, 2)], { type: 'application/json' });
    filename = 'musig2-archive-' + new Date().toISOString().slice(0, 10) + '.json';
  }

  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a); URL.revokeObjectURL(url);
  toast('İndirildi: ' + filename, 'success');
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

    // Arşiv badge'ini güncelle — yalnızca BROADCAST arşive gider
    // SIGNED aktif kalır (broadcast aksiyonu bekliyor)
    const archived = sessions.filter(s => s.state === 'BROADCAST');
    const badge = document.getElementById('dArchiveBadge');
    if (badge) badge.textContent = archived.length ? String(archived.length) : '';

    // Aktif listede BROADCAST dışı her şey görünür (SIGNED dahil)
    const active = sessions.filter(s => s.state !== 'BROADCAST');

    // localStorage'dan myPubkey otomatik detect — sayfa yenilemede polling başlatmak için
    if (!state.myPubkey) {
      for (const s of sessions) {
        const savedIdx = localStorage.getItem(D_IDX_KEY(s.id));
        if (savedIdx !== null) {
          const idx = parseInt(savedIdx);
          const pk  = s.participants?.[idx]?.pubkey;
          if (pk) {
            state.myIndex  = idx;
            state.myPubkey = pk;
            state.myRole   = idx === 0 ? 'coordinator' : 'participant';
            break;
          }
        }
      }
      if (state.myPubkey) { dStartPolling(); _dPollTick(); }
    }

    // TX dropdown için state'i güncelle
    state.dmusig2Sessions = active;
    state.dmusig2SessionsAll = sessions;
    populateWalletSelects();

    if (!active.length) {
      el.innerHTML = '<div class="empty-state">Henüz dağıtık MuSig2 oturumu yok.<br>Yeni bir oturum oluşturun.</div>';
      return;
    }
    el.innerHTML = `
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr><th>ID</th><th>Etiket</th><th>N</th><th>Ağ</th><th>Durum</th><th></th></tr></thead>
          <tbody>
            ${active.map(s => `
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
    showTab('musig2d');
    const s = await get(`/api/musig2d/${sid}`);
    state.dmusig2Session = s;

    document.getElementById('dmusig2SessionList').style.display = 'none';
    document.getElementById('dmusig2Detail').style.display = '';

    dRenderSession(s);
    document.getElementById('dmusig2Detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
    document.querySelector('.page-header button[onclick="dOpenNewSessionModal()"]').style.display = 'none';
    document.querySelector('.page-header button[onclick="openModal(\'dJoinSessionModal\')"]').style.display = 'none';
  } catch(e) {
    toast(`Oturum yüklenemedi: ${e.message}`, 'error');
  }
}

function dCloseDetail() {
  document.getElementById('dmusig2Detail').style.display = 'none';

  const newBtn  = document.querySelector('.page-header button[onclick="dOpenNewSessionModal()"]');
  const joinBtn = document.querySelector('.page-header button[onclick="openModal(\'dJoinSessionModal\')"]');
  if (newBtn)  newBtn.style.display  = '';
  if (joinBtn) joinBtn.style.display = '';

  state.dmusig2Session = null;

  if (state._archiveOpenedDetail) {
    state._archiveOpenedDetail = false;
    document.getElementById('dmusig2SessionList').style.display = 'none';
    dShowSubTab('archive');
  } else {
    document.getElementById('dmusig2SessionList').style.display = '';
    dLoadSessionList();
  }
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

  // 3F: Kaynak session referansı
  const srcRef   = document.getElementById('dmusig2SourceRef');
  const srcTxid  = document.getElementById('dmusig2SourceTxid');
  if (srcRef) {
    if (s.source_session_id) {
      srcRef.style.display = '';
      if (srcTxid) {
        srcTxid.textContent = s.source_session_id.substring(0, 8) + '…';
        srcTxid.title       = s.source_session_id;
      }
    } else {
      srcRef.style.display = 'none';
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

  // Adım göstergesi
  const STEPS = [
    { label: 'Pubkey',  states: ['COLLECTING_PUBKEYS'] },
    { label: 'TX',      states: ['READY_FOR_TX'] },
    { label: 'Nonce',   states: ['COLLECTING_NONCES'] },
    { label: 'İmza',    states: ['COLLECTING_SIGS'] },
    { label: 'Yayınla', states: ['SIGNED', 'BROADCAST'] },
  ];
  const currentStep     = STEPS.findIndex(step => step.states.includes(s.state));
  const aggregateFailed = s.state === 'COLLECTING_SIGS' &&
    s.participants.every(p => p.partial_sigs?.length > 0 && p.partial_sigs?.[0] !== null);

  const stepBarEl = document.getElementById('dmusig2StepBar');
  if (stepBarEl) {
    stepBarEl.innerHTML = `
      <div style="display:flex;align-items:center;margin-bottom:16px">
        ${STEPS.map((step, i) => {
          const done   = i < currentStep;
          const active = i === currentStep;
          const error  = active && aggregateFailed;
          const color  = done  ? '#3fb950'
                       : error ? '#f85149'
                       : active ? '#f0883e'
                       : '#30363d';
          const textColor = (done || active) ? '#e6edf3' : '#484f58';
          const anim = error ? 'animation:pulse 1.5s infinite' : '';
          const connector = i < STEPS.length - 1
            ? `<div style="height:2px;flex:1;margin-bottom:18px;background:${i < currentStep ? '#3fb950' : '#30363d'}"></div>`
            : '';
          return `
            <div style="display:flex;align-items:center;flex:1">
              <div style="display:flex;flex-direction:column;align-items:center;flex:1;gap:4px">
                <div style="width:28px;height:28px;border-radius:50%;background:${color};
                  display:flex;align-items:center;justify-content:center;
                  font-size:0.75em;font-weight:700;color:#0d1117;${anim}">
                  ${done ? '✓' : error ? '!' : i + 1}
                </div>
                <span style="font-size:0.7em;color:${textColor};white-space:nowrap">${step.label}</span>
              </div>
              ${connector}
            </div>`;
        }).join('')}
      </div>`;
  }

  // Katılımcı tablosu
  const tbody = document.getElementById('dmusig2ParticipantTable');
  tbody.innerHTML = s.participants.map((p, i) => {
    const pkShort = p.pubkey ? p.pubkey.slice(0,12) + '…' : '—';
    const nonceOk = p.pubnonces?.length > 0 && p.pubnonces?.[0] !== null;
    const sigOk   = p.partial_sigs?.length > 0 && p.partial_sigs?.[0] !== null;
    const nonceIcon = nonceOk ? '<span style="color:#3fb950">✓</span>' : '—';
    const sigIcon   = sigOk
      ? (aggregateFailed
          ? '<span title="Gönderildi fakat aggregate başarısız" style="color:#d29922">⚠</span>'
          : '<span style="color:#3fb950">✓</span>')
      : '—';
    const rowStyle = aggregateFailed && sigOk ? 'background:#1a1000;' : '';
    return `<tr style="${rowStyle}">
      <td>${i+1}</td>
      <td>${p.label}</td>
      <td class="mono-sm" style="font-size:0.78em">${pkShort}</td>
      <td>${nonceIcon}</td>
      <td>${sigIcon}</td>
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
    // Bekleyen katılımcıyı otomatik seç:
    // COLLECTING_NONCES → nonce'ını henüz göndermemiş ilk katılımcı
    // COLLECTING_SIGS   → imzasını henüz göndermemiş ilk katılımcı
    // COLLECTING_PUBKEYS → pubkey'ini henüz kaydetmemiş ilk katılımcı
    let autoIdx = 0;
    if (s.state === 'COLLECTING_NONCES') {
      const i = s.participants.findIndex(p =>
        !p.pubnonces || p.pubnonces.length === 0 || p.pubnonces.every(n => n === null));
      if (i >= 0) autoIdx = i;
    } else if (s.state === 'COLLECTING_SIGS') {
      const i = s.participants.findIndex(p =>
        !p.partial_sigs || p.partial_sigs.length === 0 || p.partial_sigs.every(sig => sig === null));
      if (i >= 0) autoIdx = i;
    } else if (s.state === 'COLLECTING_PUBKEYS') {
      const i = s.participants.findIndex(p => !p.pubkey);
      if (i >= 0) autoIdx = i;
    }
    idxSel.value = String(autoIdx);
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

  // ── Farklı tarayıcı uyarısı ───────────────────────────────────────────────
  // Koşul: savedIdx yok + tüm pubkey'ler kayıtlı + nonce/sig aşaması
  // ANCAK: bu tarayıcıda başka bir session'dan bilinen pubkey eşleşiyorsa meşru katılımcı.
  // (Örnek: dQuickSession'da yeni session ID farklı olur, eski session'ın D_IDX_KEY'i kopyalanmaz)
  const allPubkeysRegistered = s.participants.every(p => p.pubkey);
  const sessionPubkeys = new Set(s.participants.map(p => p.pubkey).filter(Boolean));

  // Bu tarayıcıda kayıtlı tüm pubkey'leri topla (tüm session'lardaki D_IDX_KEY'lerden)
  const knownPubkeys = new Set();
  let autoDetectedIdx = null;
  for (let i = 0; i < localStorage.length; i++) {
    const lsKey = localStorage.key(i);
    if (!lsKey.startsWith('dmusig2_idx_')) continue;
    const lsSid = lsKey.replace('dmusig2_idx_', '');
    if (lsSid === s.id) continue;  // bu session zaten savedIdx=null → atla
    const lsIdx = parseInt(localStorage.getItem(lsKey));
    const oldSession = state.dmusig2SessionsAll?.find(x => x.id === lsSid);
    const oldPk = oldSession?.participants?.[lsIdx]?.pubkey;
    if (oldPk && sessionPubkeys.has(oldPk)) {
      knownPubkeys.add(oldPk);
      // Bu tarayıcı meşru katılımcı — index'ini de tespit et
      const matchIdx = s.participants.findIndex(p => p.pubkey === oldPk);
      if (matchIdx >= 0) autoDetectedIdx = matchIdx;
    }
  }

  const isLegitimate = knownPubkeys.size > 0;  // Bu tarayıcı başka session'dan tanınan katılımcı
  const isWrongBrowser = savedIdx === null && allPubkeysRegistered &&
                         ['COLLECTING_NONCES', 'COLLECTING_SIGS'].includes(s.state) &&
                         !isLegitimate;

  // Meşru katılımcı tespit edildiyse D_IDX_KEY'i otomatik yaz ve dropdown'u kilitle
  if (isLegitimate && savedIdx === null && autoDetectedIdx !== null) {
    localStorage.setItem(D_IDX_KEY(s.id), String(autoDetectedIdx));
    idxSel.value    = String(autoDetectedIdx);
    idxSel.disabled = true;
    idxSel.title    = 'Önceki oturumdan tanındı — katılımcı kimliği kilitli';
    state.myIndex   = autoDetectedIdx;
    state.myRole    = autoDetectedIdx === 0 ? 'coordinator' : 'participant';
    state.myPubkey  = s.participants[autoDetectedIdx]?.pubkey || null;
    if (state.myPubkey && !document.hidden) dStartPolling();
    console.log(`[dMusig2] Meşru katılımcı otomatik tespit: idx=${autoDetectedIdx} pubkey=${state.myPubkey?.substring(0,16)}…`);
  }

  // Console log — sadece state değiştiğinde bas (polling tekrarında sustur)
  if (isWrongBrowser !== state._wrongBrowser) {
    console.log(`[dMusig2] WrongBrowser check | session=${s.id} state=${s.state}`, {
      savedIdx, allPubkeysRegistered, isLegitimate,
      knownPubkeys: [...knownPubkeys].map(p => p.substring(0,16) + '…'),
      autoDetectedIdx, isWrongBrowser,
    });
  }

  state._wrongBrowser = isWrongBrowser;
  const wbWarning = document.getElementById('dmusig2WrongBrowserWarning');
  if (wbWarning) wbWarning.style.display = isWrongBrowser ? '' : 'none';

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

    // Yayınla butonu: BROADCAST state'te gizle
    const broadcastBtn = document.getElementById('dmusig2BroadcastBtn');
    if (broadcastBtn) broadcastBtn.style.display = s.state === 'BROADCAST' ? 'none' : '';

    // 4J: TXID kalıcı satırı
    const txidRow      = document.getElementById('dmusig2TxidRow');
    const txidVal      = document.getElementById('dmusig2TxidValue');
    const txidExplorer = document.getElementById('dmusig2TxidExplorer');
    if (txidRow) {
      if (s.txid) {
        txidRow.style.display = '';
        if (txidVal)      txidVal.textContent = s.txid;
        if (txidExplorer) {
          const base = s.network === 'mainnet'
            ? 'https://mempool.space/tx/'
            : 'https://mempool.space/testnet4/tx/';
          txidExplorer.href = base + s.txid;
        }
      } else {
        txidRow.style.display = 'none';
      }
    }

    // 3A: Kalan bakiyeyi kontrol et
    if (s.state === 'BROADCAST') dCheckChangeBalance(s);
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
    const hasNonce = isKnown &&
      s.participants[myIdx]?.pubnonces?.length > 0 &&
      s.participants[myIdx]?.pubnonces?.[0] !== null;
    if (!hasNonce) {
      btns.push(`<button class="btn btn-primary" onclick="dSubmitNonce()">Nonce Üret & Gönder</button>`);
    } else {
      btns.push(`<span style="color:#8b949e;font-size:0.85em">⌛ Diğer nonce\'lar bekleniyor</span>`);
    }
  }
  if (s.state === 'COLLECTING_SIGS') {
    const hasSig = isKnown &&
      s.participants[myIdx]?.partial_sigs?.length > 0 &&
      s.participants[myIdx]?.partial_sigs?.[0] !== null;
    const allSigsPresent = s.participants.every(
      p => p.partial_sigs?.length > 0 && p.partial_sigs?.[0] !== null
    );
    if (allSigsPresent) {
      // Tüm imzalar toplandı ama aggregate başarısız — inline hata paneli
      btns.push(`
        <div style="background:#1a0a0a;border:1px solid #f85149;border-radius:6px;
          padding:10px 14px;margin-bottom:8px;font-size:0.85em">
          <div style="color:#f85149;font-weight:600;margin-bottom:4px">⚠ İmzalama tamamlanamadı</div>
          <div style="color:#8b949e;line-height:1.5">
            Her iki katılımcının imzası uyumsuz çıktı.
            Koordinatör nonce sıfırlamalı, ardından her iki taraf yeniden nonce ve imza göndermelidir.
          </div>
          <details style="margin-top:6px">
            <summary style="color:#484f58;cursor:pointer;font-size:0.9em">Teknik detay</summary>
            <code style="color:#484f58;font-size:0.8em">Schnorr aggregate doğrulaması başarısız (Q_even_y uyumsuzluğu)</code>
          </details>
        </div>`);
      if (isCoord) {
        btns.push(`
          <div id="retryConfirmBox" style="display:none;background:#1a0a0a;border:1px solid #d29922;
            border-radius:6px;padding:10px 14px;margin-bottom:8px;font-size:0.84em;color:#d29922">
            Bu işlem her iki katılımcının nonce ve imzasını siler.
            Koordinatör yeni nonce gönderince diğer katılımcı da otomatik uyarılır.
            Devam edilsin mi?
            <div style="margin-top:8px;display:flex;gap:8px">
              <button class="btn btn-danger sm" onclick="dResetAndRetry()">Evet, Sıfırla</button>
              <button class="btn btn-ghost sm"
                onclick="document.getElementById('retryConfirmBox').style.display='none';
                         document.getElementById('retryBtn').style.display=''">
                Vazgeç
              </button>
            </div>
          </div>
          <button id="retryBtn" class="btn btn-orange" onclick="
            document.getElementById('retryBtn').style.display='none';
            document.getElementById('retryConfirmBox').style.display=''">
            ↺ Nonce Sıfırla & Yeniden Dene
          </button>`);
      } else {
        btns.push(`<span style="color:#8b949e;font-size:0.85em">⌛ Koordinatör nonce sıfırlamasını başlatmayı bekleyin</span>`);
      }
    } else if (!hasSig) {
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

  // 4N: Export menüsünü tıklama dışına basınca kapat
  document.addEventListener('click', e => {
    const menu   = document.getElementById('dExportMenu');
    const toggle = document.getElementById('dExportToggle');
    if (menu && toggle && !toggle.contains(e.target)) {
      menu.style.display = 'none';
    }
  });
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
  if (state._wrongBrowser) { toast('⛔ Farklı tarayıcı — nonce üretimi bu tarayıcıda mümkün değil', 'error'); return; }
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
  if (state._wrongBrowser) { toast('⛔ Farklı tarayıcı — imzalama bu tarayıcıda mümkün değil', 'error'); return; }
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
    uiLog(`partial_sig hata: ${e.message}`, 'ERR');
    // Toast kaldırıldı — hata UI'ı dRenderActionButtons üstleniyor
    // Backend sig'i kaydetmiş olabilir; güncel state'i çek
    try {
      const refreshed = await get(`/api/musig2d/${s.id}`);
      state.dmusig2Session = refreshed;
      dRenderSession(refreshed);
    } catch(_) {}
  }
}

// ── Nonce Sıfırla & Yeniden Dene ───────────────────────────────────────────

async function dResetAndRetry() {
  const s = state.dmusig2Session;
  if (!s) return;

  const counterKey = `dmusig2_retry_${s.id}`;
  const retryCount = parseInt(sessionStorage.getItem(counterKey) || '0');
  const newCount   = retryCount + 1;
  sessionStorage.setItem(counterKey, String(newCount));

  // 5. denemeden sonra — yeni oturum yönlendirmesi
  if (newCount > 5) {
    document.getElementById('dmusig2ActionButtons').innerHTML = `
      <div style="background:#1a0505;border:1px solid #f85149;border-radius:6px;
        padding:12px 14px;font-size:0.85em">
        <div style="color:#f85149;font-weight:600;margin-bottom:6px">
          ⛔ Tekrarlayan imzalama hatası (${newCount}. deneme)
        </div>
        <div style="color:#8b949e;margin-bottom:10px">
          Bu oturumda imzalama defalarca başarısız oldu.
          Muhtemelen kalıcı bir kriptografik uyumsuzluk var.
          Yeni oturum açmanız önerilir.
        </div>
        <button class="btn btn-ghost sm" onclick="dCloseDetail()">← Oturum Listesine Dön</button>
      </div>`;
    return;
  }

  if (newCount === 3) {
    uiLog(`Uyarı: ${s.label} için ${newCount}. nonce sıfırlama denemesi`, 'WARN');
  }

  try {
    const updated = await post(`/api/musig2d/${s.id}/reset-nonces`, {});
    for (let i = 0; i < (s.sighashes?.length || 1); i++) {
      localStorage.removeItem(D_NONCE_KEY(s.id, i));
    }
    state.dmusig2Session = updated;
    dRenderSession(updated);
    toast(
      newCount >= 3
        ? `Sıfırlandı (${newCount}. deneme) — dikkatli ilerleyin`
        : "Nonce'lar sıfırlandı — yeniden nonce gönderin",
      newCount >= 3 ? '' : 'success'
    );
  } catch(e) {
    toast(`Sıfırlama başarısız: ${e.message}`, 'error');
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
    if (res.txid) state.dmusig2Session.txid = res.txid;  // 4K: local state'e yaz
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
