// Dashboard JavaScript

// State
let orders = [];
let positions = [];
let symbols = [];
let currentTab = 'orders';

// URLs
const baseUrl = window.location.origin;
const webhookUrl = baseUrl + '/order';
const webhookUrlReal = baseUrl + '/order?simulation=false';

// Labels
const actionIcons = { 
    long_entry: { icon: '📈', label: '多入', color: '#00ff88' },
    long_exit: { icon: '📤', label: '多出', color: '#00d9ff' },
    short_entry: { icon: '📉', label: '空入', color: '#ff6b6b' },
    short_exit: { icon: '📥', label: '空出', color: '#ffc107' }
};
const actionLabels = { 
    long_entry: '做多進場', 
    long_exit: '做多出場', 
    short_entry: '做空進場', 
    short_exit: '做空出場' 
};
const statusLabels = { 
    pending: '待處理', 
    submitted: '委託中', 
    filled: '已成交', 
    partial_filled: '部分成交',
    cancelled: '已取消',
    failed: '失敗', 
    no_action: '無動作',
    success: '成功'
};
const fillStatusLabels = {
    PendingSubmit: '待送出',
    PreSubmitted: '預送出',
    Submitted: '委託中',
    Filled: '已成交',
    PartFilled: '部分成交',
    Cancelled: '已取消',
    Failed: '失敗'
};
const dirLabels = { buy: '買', sell: '賣', Buy: '買', Sell: '賣' };

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('webhookUrl').textContent = webhookUrl;
    document.getElementById('authKey').addEventListener('keypress', (e) => { 
        if (e.key === 'Enter') loadCurrentTab(); 
    });
    fetchSymbols();
});

// Trading Mode Toggle
function toggleTradingMode() {
    const toggle = document.getElementById('modeToggle');
    const webhookUrlEl = document.getElementById('webhookUrl');
    const webhookCard = document.getElementById('webhookCard');
    const realWarning = document.getElementById('realTradingWarning');
    const simInfo = document.getElementById('simModeInfo');
    const simLabel = document.getElementById('simLabel');
    const realLabel = document.getElementById('realLabel');
    
    if (toggle.checked) {
        webhookUrlEl.textContent = webhookUrlReal;
        webhookCard.classList.add('real-trading-mode');
        realWarning.style.display = 'block';
        simInfo.style.display = 'none';
        simLabel.style.color = '#71717a';
        realLabel.style.color = '#ef4444';
        realLabel.style.fontWeight = '600';
        simLabel.style.fontWeight = 'normal';
    } else {
        webhookUrlEl.textContent = webhookUrl;
        webhookCard.classList.remove('real-trading-mode');
        realWarning.style.display = 'none';
        simInfo.style.display = 'block';
        simLabel.style.color = '#22c55e';
        realLabel.style.color = '#71717a';
        simLabel.style.fontWeight = '600';
        realLabel.style.fontWeight = 'normal';
    }
}

function copyWebhookUrl() {
    const toggle = document.getElementById('modeToggle');
    const url = toggle.checked ? webhookUrlReal : webhookUrl;
    navigator.clipboard.writeText(url).then(() => {
        const btn = document.querySelector('#webhookCodeBlock .copy-btn');
        const original = btn.textContent;
        btn.textContent = '已複製！';
        btn.style.background = '#22c55e';
        setTimeout(() => {
            btn.textContent = original;
            btn.style.background = '';
        }, 2000);
    });
}

// Tab Navigation
function switchTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelector(`.tab[onclick="switchTab('${tab}')"]`).classList.add('active');
    document.getElementById(`${tab}-tab`).classList.add('active');
}

function loadCurrentTab() {
    if (currentTab === 'orders') fetchOrders();
    else if (currentTab === 'positions') fetchPositions();
    else if (currentTab === 'symbols') fetchSymbols();
}

// Orders
async function fetchOrders() {
    const authKey = document.getElementById('authKey').value;
    if (!authKey) { showError('請輸入驗證金鑰'); return; }
    
    const status = document.getElementById('filterStatus').value;
    const action = document.getElementById('filterAction').value;
    const symbol = document.getElementById('filterSymbol').value;
    
    let url = '/orders?limit=500';
    if (status) url += `&status=${status}`;
    if (action) url += `&action=${action}`;
    if (symbol) url += `&symbol=${symbol}`;
    
    document.getElementById('ordersTable').innerHTML = '<div class="loading">載入中...</div>';
    hideError();
    
    try {
        const response = await fetch(url, { headers: { 'X-Auth-Key': authKey } });
        if (!response.ok) throw new Error(response.status === 401 ? '驗證金鑰無效' : '載入失敗');
        orders = await response.json();
        renderOrdersTable();
        updateOrderStats();
    } catch (error) {
        showError(error.message);
        document.getElementById('ordersTable').innerHTML = '<div class="empty">載入失敗</div>';
    }
}

function renderOrdersTable() {
    if (orders.length === 0) {
        document.getElementById('ordersTable').innerHTML = '<div class="empty">無委託紀錄</div>';
        return;
    }
    
    let html = `<table><thead><tr>
        <th style="width:100px">時間</th>
        <th style="width:40px">#</th>
        <th style="width:150px">商品</th>
        <th style="width:60px">動作</th>
        <th style="width:40px">口數</th>
        <th style="width:65px">狀態</th>
        <th style="width:120px">成交</th>
        <th>訊息</th>
        <th style="width:40px"></th>
    </tr></thead><tbody>`;
    
    for (const order of orders) {
        const d = new Date(order.created_at);
        const date = `${d.getMonth()+1}/${d.getDate()} ${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}`;
        
        const statusClass = order.status === 'filled' ? 'status-success' : 
                           order.status === 'failed' ? 'status-failed' :
                           order.status === 'cancelled' || order.status === 'no_action' ? 'status-no_action' :
                           'status-pending';
        const statusText = statusLabels[order.status] || order.status;
        
        const fillInfo = order.fill_quantity 
            ? `${order.fill_quantity}口 @ ${order.fill_price?.toLocaleString() || '-'}` 
            : '-';
        
        const act = actionIcons[order.action] || { icon: '●', label: order.action, color: '#a1a1aa' };
        
        const canRecheck = ['submitted', 'pending', 'partial_filled'].includes(order.status);
        const recheckBtn = canRecheck 
            ? `<button class="recheck-btn" onclick="recheckOrder(${order.id})" title="重新查詢狀態">🔄</button>`
            : '';
        
        // Error message - truncate if too long
        const errorMsg = order.error_message 
            ? (order.error_message.length > 50 
                ? `<span title="${order.error_message}" style="color:#ff6b6b;font-size:0.8rem;cursor:help">${order.error_message.substring(0, 50)}...</span>`
                : `<span style="color:#ff6b6b;font-size:0.8rem">${order.error_message}</span>`)
            : '<span style="color:#52525b">-</span>';
        
        html += `<tr id="order-row-${order.id}">
            <td style="color:#a1a1aa;font-size:0.8rem;font-family:'Consolas',monospace">${date}</td>
            <td style="color:#71717a;font-size:0.8rem">${order.id}</td>
            <td>
                <div style="font-family:'Consolas',monospace">
                    <span style="color:#00d9ff;font-weight:600;font-size:0.85rem">${order.symbol}</span>
                    ${order.code && order.code !== order.symbol ? `<br><span style="color:#71717a;font-size:0.7rem">${order.code}</span>` : ''}
                </div>
            </td>
            <td>
                <span style="color:${act.color};font-size:0.85rem" title="${actionLabels[order.action] || order.action}">${act.icon}${act.label}</span>
            </td>
            <td style="text-align:center;font-weight:600">${order.quantity}</td>
            <td><span class="status ${statusClass}">${statusText}</span></td>
            <td style="font-family:'Consolas',monospace;font-size:0.8rem">${fillInfo}</td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${errorMsg}</td>
            <td>${recheckBtn}</td>
        </tr>`;
    }
    html += '</tbody></table>';
    document.getElementById('ordersTable').innerHTML = html;
}

async function recheckOrder(orderId) {
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = '⏳';
    
    const authKey = document.getElementById('authKey').value;
    const simulationMode = document.getElementById('simulationMode').checked;
    
    if (!authKey) {
        alert('請先輸入驗證金鑰');
        btn.disabled = false;
        btn.textContent = '🔄';
        return;
    }
    
    try {
        const response = await fetch(`/orders/${orderId}/recheck?simulation=${simulationMode}`, {
            method: 'POST',
            headers: { 'X-Auth-Key': authKey }
        });
        
        const result = await response.json();
        
        if (!response.ok) {
            throw new Error(result.detail || '查詢失敗');
        }
        
        let msg = `訂單 #${orderId} 狀態更新:\n`;
        msg += `• 狀態: ${result.previous_status} → ${result.current_status}\n`;
        msg += `• 交易所狀態: ${result.current_fill_status}\n`;
        if (result.fill_quantity > 0) {
            msg += `• 成交: ${result.fill_quantity} 口 @ ${result.fill_price?.toFixed(2) || '-'}\n`;
        }
        if (result.deals && result.deals.length > 0) {
            msg += `• 成交明細: ${result.deals.length} 筆`;
        }
        
        alert(msg);
        await fetchOrders();
        
    } catch (error) {
        alert(`查詢失敗: ${error.message}`);
        btn.disabled = false;
        btn.textContent = '🔄';
    }
}

function updateOrderStats() {
    document.getElementById('statTotal').textContent = orders.length;
    document.getElementById('statSuccess').textContent = orders.filter(o => o.status === 'filled' || o.status === 'success').length;
    document.getElementById('statFailed').textContent = orders.filter(o => o.status === 'failed').length;
}

// Positions
async function fetchPositions() {
    const authKey = document.getElementById('authKey').value;
    if (!authKey) { showError('請輸入驗證金鑰'); return; }
    
    document.getElementById('positionsTable').innerHTML = '<div class="loading">載入中...</div>';
    hideError();
    
    try {
        const response = await fetch('/positions', { headers: { 'X-Auth-Key': authKey } });
        if (!response.ok) throw new Error(response.status === 401 ? '驗證金鑰無效' : '載入失敗');
        const data = await response.json();
        positions = data.positions;
        renderPositionsTable();
        updatePositionStats();
    } catch (error) {
        showError(error.message);
        document.getElementById('positionsTable').innerHTML = '<div class="empty">載入失敗</div>';
    }
}

function renderPositionsTable() {
    if (positions.length === 0) {
        document.getElementById('positionsTable').innerHTML = '<div class="empty">目前無持倉</div>';
        return;
    }
    
    let html = `<table><thead><tr>
        <th>商品</th>
        <th style="width:70px">方向</th>
        <th style="width:60px">口數</th>
        <th>均價</th>
        <th>現價</th>
        <th>損益</th>
    </tr></thead><tbody>`;
    
    for (const pos of positions) {
        const pnlClass = pos.pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
        const isLong = pos.direction.toLowerCase() === 'buy';
        const dirIcon = isLong ? '🟢' : '🔴';
        const dirText = isLong ? '多' : '空';
        const dirColor = isLong ? '#00ff88' : '#ff6b6b';
        
        html += `<tr>
            <td>
                <div style="font-family:'Consolas',monospace">
                    <span style="color:#00d9ff;font-weight:600">${pos.symbol}</span>
                    ${pos.code && pos.code !== pos.symbol ? `<span style="color:#71717a;font-size:0.75rem;margin-left:4px">${pos.code}</span>` : ''}
                </div>
            </td>
            <td><span style="color:${dirColor}">${dirIcon} ${dirText}</span></td>
            <td style="text-align:center;font-weight:600">${pos.quantity}</td>
            <td style="font-family:'Consolas',monospace">${pos.price.toLocaleString()}</td>
            <td style="font-family:'Consolas',monospace">${pos.last_price.toLocaleString()}</td>
            <td class="${pnlClass}" style="font-weight:600">${pos.pnl >= 0 ? '+' : ''}${pos.pnl.toLocaleString()}</td>
        </tr>`;
    }
    html += '</tbody></table>';
    document.getElementById('positionsTable').innerHTML = html;
}

function updatePositionStats() {
    const totalPnl = positions.reduce((sum, p) => sum + p.pnl, 0);
    document.getElementById('posCount').textContent = positions.length;
    document.getElementById('totalPnl').textContent = (totalPnl >= 0 ? '+' : '') + totalPnl.toLocaleString();
    const pnlCard = document.getElementById('pnlCard');
    pnlCard.className = 'stat-card ' + (totalPnl >= 0 ? 'pnl-positive' : 'pnl-negative');
}

// Symbols
async function fetchSymbols() {
    const simulationMode = document.getElementById('simulationMode').checked;
    document.getElementById('symbolsTable').innerHTML = '<div class="loading">載入中...</div>';
    
    try {
        const response = await fetch(`/symbols?simulation=${simulationMode}`);
        if (!response.ok) throw new Error('無法取得商品列表');
        
        const data = await response.json();
        symbols = data.symbols || [];
        renderSymbolsTable();
        updateSymbolStats();
        hideError();
    } catch (error) {
        document.getElementById('symbolsTable').innerHTML = `<div class="empty" style="color:#ff6b6b">載入失敗: ${error.message}</div>`;
    }
}

function filterSymbols() {
    const search = document.getElementById('symbolSearch').value.toLowerCase();
    const filtered = symbols.filter(s => 
        s.symbol.toLowerCase().includes(search) || 
        s.code.toLowerCase().includes(search) ||
        s.name.toLowerCase().includes(search)
    );
    renderSymbolsTable(filtered);
}

function renderSymbolsTable(list = symbols) {
    if (list.length === 0) {
        document.getElementById('symbolsTable').innerHTML = '<div class="empty">無符合的商品</div>';
        return;
    }
    
    let html = `<table>
        <thead>
            <tr>
                <th>Symbol (用於下單)</th>
                <th>Code (交易所代碼)</th>
                <th>名稱</th>
                <th>操作</th>
            </tr>
        </thead>
        <tbody>`;
    for (const item of list) {
        html += `<tr>
            <td><strong style="color: #00d9ff; font-family: 'Consolas', monospace;">${item.symbol}</strong></td>
            <td style="color: #a1a1aa; font-family: 'Consolas', monospace;">${item.code}</td>
            <td>${item.name}</td>
            <td><button class="recheck-btn" onclick="copySymbol('${item.symbol}')">📋 複製</button></td>
        </tr>`;
    }
    html += '</tbody></table>';
    document.getElementById('symbolsTable').innerHTML = html;
}

function copySymbol(symbol) {
    navigator.clipboard.writeText(symbol).then(() => {
        const btn = event.target;
        const original = btn.textContent;
        btn.textContent = '✓ 已複製';
        btn.style.background = 'rgba(0, 255, 136, 0.3)';
        btn.style.borderColor = '#00ff88';
        setTimeout(() => {
            btn.textContent = original;
            btn.style.background = '';
            btn.style.borderColor = '';
        }, 1500);
    });
}

function updateSymbolStats() {
    document.getElementById('symbolCount').textContent = symbols.length;
}

// Utilities
function exportCSV() {
    const authKey = document.getElementById('authKey').value;
    if (!authKey) { showError('請輸入驗證金鑰'); return; }
    window.open('/orders/export?format=csv', '_blank');
}

function copyToClipboard(btn, text) {
    navigator.clipboard.writeText(text).then(() => {
        const original = btn.textContent;
        btn.textContent = '已複製！';
        btn.style.color = '#00ff88';
        setTimeout(() => { btn.textContent = original; btn.style.color = ''; }, 2000);
    });
}

function showError(msg) { 
    const el = document.getElementById('errorMsg'); 
    el.textContent = msg; 
    el.style.display = 'block'; 
}

function hideError() { 
    document.getElementById('errorMsg').style.display = 'none'; 
}

