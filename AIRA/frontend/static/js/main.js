const AIRA = {
    currentConversation: null,
    isGenerating: false,
    activities: [],

    init() {
        this.bindEvents();
        this.checkStatus();
        this.loadConversations();
        setInterval(() => this.checkStatus(), 30000);
    },

    bindEvents() {
        document.getElementById('chatForm').addEventListener('submit', (e) => {
            e.preventDefault();
            this.sendMessage();
        });

        document.getElementById('chatInput').addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendMessage();
            }
        });

        document.getElementById('btnNewChat').addEventListener('click', () => this.newConversation());
        document.getElementById('btnDashboard').addEventListener('click', () => this.toggleDashboard());
        document.getElementById('btnStop').addEventListener('click', () => this.stopGeneration());
        document.getElementById('btnPCAgent').addEventListener('click', () => this.togglePCPanel());
    },

    async checkStatus() {
        try {
            const resp = await fetch('/api/status');
            const data = await resp.json();
            const dot = document.getElementById('statusDot');
            const label = document.getElementById('statusLabel');

            if (data.ready && data.ai_available) {
                dot.className = 'status-dot';
                label.textContent = `Online · ${data.ai_model}`;
            } else if (data.ready) {
                dot.className = 'status-dot loading';
                label.textContent = 'Ready (No AI Key)';
            } else {
                dot.className = 'status-dot offline';
                label.textContent = 'Initializing...';
            }

            this.updateDashboardStats(data);
        } catch {
            document.getElementById('statusDot').className = 'status-dot offline';
            document.getElementById('statusLabel').textContent = 'Offline';
        }
    },

    updateDashboardStats(data) {
        if (!data.memory_stats) return;
        const stats = data.memory_stats;
        this.setText('statProvider', data.ai_provider || '-');
        this.setText('statModel', data.ai_model || '-');
        this.setText('statConversations', stats.conversations || 0);
        this.setText('statMessages', stats.messages || 0);
        this.setText('statKnowledge', stats.knowledge || 0);
        this.setText('statVerified', stats.verified_knowledge || 0);
        this.setText('statErrors', stats.unresolved_errors || 0);
        this.setText('statSkills', (data.skills || []).length);
    },

    setText(id, text) {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    },

    async loadConversations() {
        try {
            const resp = await fetch('/api/conversations');
            const data = await resp.json();
            const list = document.getElementById('conversationList');
            list.innerHTML = '';
            (data.conversations || []).forEach(c => {
                const div = document.createElement('div');
                div.className = `conv-item ${this.currentConversation === c.id ? 'active' : ''}`;
                div.innerHTML = `<span class="conv-icon">💬</span>${this.escapeHtml(c.title || 'Untitled')}`;
                div.onclick = () => this.loadConversation(c.id);
                list.appendChild(div);
            });
        } catch (e) {
            console.error('Failed to load conversations:', e);
        }
    },

    async newConversation() {
        try {
            const resp = await fetch('/api/conversations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: 'New Chat' }),
            });
            const data = await resp.json();
            this.currentConversation = data.conversation_id;
            document.getElementById('chatMessages').innerHTML = '';
            this.showWelcome(false);
            await this.loadConversations();
        } catch (e) {
            console.error('Failed to create conversation:', e);
        }
    },

    async loadConversation(id) {
        this.currentConversation = id;
        try {
            const resp = await fetch(`/api/conversations/${id}/messages`);
            const data = await resp.json();
            const container = document.getElementById('chatMessages');
            container.innerHTML = '';
            this.showWelcome(false);

            (data.messages || []).forEach(m => {
                this.appendMessage(m.role, m.content, m.timestamp, false);
            });

            await this.loadConversations();
            this.scrollToBottom();
        } catch (e) {
            console.error('Failed to load conversation:', e);
        }
    },

    showWelcome(show) {
        const ws = document.getElementById('welcomeState');
        const cm = document.getElementById('chatMessages');
        if (ws) ws.style.display = show ? 'flex' : 'none';
        if (cm) cm.style.display = show ? 'none' : 'flex';
    },

    togglePCPanel() {
        const panel = document.getElementById('pcAgentPanel');
        panel.classList.toggle('active');
        if (panel.classList.contains('active')) this.refreshPCPanel();
    },

    async sendMessage() {
        const input = document.getElementById('chatInput');
        const sendButton = document.getElementById('btnSend');
        const stopButton = document.getElementById('btnStop');

        if (!input) {
            console.error('chatInput element not found');
            return;
        }

        const message = input.value.trim();
        if (!message || this.isGenerating) return;

        input.value = '';
        this.showWelcome(false);
        this.appendMessage('user', message);
        this.scrollToBottom();

        this.isGenerating = true;

        if (sendButton) sendButton.style.display = 'none';
        if (stopButton) stopButton.style.display = 'block';

        const typing = this.appendTyping();
        this.scrollToBottom();

        try {
            const resp = await fetch('/api/chat/stream', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'text/event-stream'
                },
                body: JSON.stringify({
                    message: message,
                    conversation_id: this.currentConversation,
                }),
            });

            if (!resp.ok) {
                let errorMessage = `HTTP ${resp.status}`;

                try {
                    const errorText = await resp.text();
                    if (errorText) {
                        try {
                            const errorJson = JSON.parse(errorText);
                            errorMessage =
                                errorJson.detail ||
                                errorJson.message ||
                                errorJson.error ||
                                errorText;
                        } catch {
                            errorMessage = errorText;
                        }
                    }
                } catch {}

                throw new Error(errorMessage);
            }

            if (!resp.body) {
                throw new Error('No response stream received from AIRA.');
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let fullResponse = '';
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';

                for (const rawLine of lines) {
                    const line = rawLine.trimEnd();

                    if (!line.startsWith('data: ')) continue;

                    const data = line.slice(6).trim();

                    if (!data || data === '[DONE]') continue;

                    if (data.startsWith('{')) {
                        try {
                            const obj = JSON.parse(data);

                            if (obj.conversation_id) {
                                this.currentConversation = obj.conversation_id;
                            }

                            if (obj.error) {
                                throw new Error(obj.error);
                            }

                            if (obj.message && !fullResponse) {
                                fullResponse = obj.message;
                                this.updateLastMessage(fullResponse);
                            }
                        } catch (parseError) {
                            if (parseError instanceof Error &&
                                parseError.message &&
                                !parseError.message.includes('Unexpected token')) {
                                throw parseError;
                            }
                        }
                    } else if (!data.startsWith('[')) {
                        fullResponse += data;
                        this.updateLastMessage(fullResponse);
                        this.scrollToBottom();
                    }
                }
            }

            typing.remove();

            if (fullResponse) {
                this.appendMessage('assistant', fullResponse);
            } else {
                this.appendMessage(
                    'assistant',
                    'AIRA did not return a response.'
                );
            }

            this.scrollToBottom();
            this.addActivity('Message sent and responded');
            await this.loadConversations();

        } catch (e) {
            if (typing) typing.remove();

            const errorMessage =
                e instanceof Error ? e.message : String(e);

            console.error('Chat error:', e);

            this.appendMessage(
                'assistant',
                `Error: ${errorMessage}`
            );

        } finally {
            this.isGenerating = false;

            if (sendButton) sendButton.style.display = 'block';
            if (stopButton) stopButton.style.display = 'none';
        }
    },
    stopGeneration() {
        this.isGenerating = false;
        document.getElementById('btnSend').style.display = 'block';
        document.getElementById('btnStop').style.display = 'none';
    },

    appendMessage(role, content, timestamp = null, animate = true) {
        const container = document.getElementById('chatMessages');
        const div = document.createElement('div');
        div.className = `message ${role === 'user' ? 'user' : 'ai'}`;

        const time = timestamp
            ? new Date(timestamp).toLocaleTimeString()
            : new Date().toLocaleTimeString();

        div.innerHTML = `
            <div class="msg-label">${role === 'user' ? 'You' : 'AIRA'}</div>
            <div class="msg-content">${this.escapeHtml(content)}</div>
            <div class="msg-time">${time}</div>
        `;

        if (!animate) div.style.animation = 'none';
        container.appendChild(div);
        return div;
    },

    updateLastMessage(content) {
        const container = document.getElementById('chatMessages');
        if (!container) return;

        const messages = container.querySelectorAll('.message.ai');
        if (messages.length === 0) return;

        const last = messages[messages.length - 1];
        let contentEl = last.querySelector('.msg-content');

        // The newest AI message may still be the typing indicator.
        // Create the content element before attempting to update it.
        if (!contentEl) {
            const typing = last.querySelector('.typing-indicator');
            if (typing) typing.remove();

            contentEl = document.createElement('div');
            contentEl.className = 'msg-content';
            last.appendChild(contentEl);

            const timeEl = document.createElement('div');
            timeEl.className = 'msg-time';
            timeEl.textContent = new Date().toLocaleTimeString();
            last.appendChild(timeEl);
        }

        contentEl.textContent = content;
    },

    appendTyping() {
        const container = document.getElementById('chatMessages');
        const div = document.createElement('div');
        div.className = 'message ai';
        div.innerHTML = `
            <div class="msg-label">AIRA</div>
            <div class="typing-indicator">
                <span></span><span></span><span></span>
            </div>
        `;
        container.appendChild(div);
        return div;
    },

    scrollToBottom() {
        const container = document.getElementById('chatMessages');
        setTimeout(() => container.scrollTop = container.scrollHeight, 50);
    },

    toggleDashboard() {
        const panel = document.getElementById('dashboardPanel');
        panel.classList.toggle('active');
        if (panel.classList.contains('active')) this.refreshDashboard();
    },

    async refreshDashboard() {
        try {
            const resp = await fetch('/api/status');
            const data = await resp.json();
            this.updateDashboardStats(data);
        } catch {}
    },

    addActivity(text) {
        const now = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        this.activities.unshift({ time: now, text });
        if (this.activities.length > 20) this.activities.pop();
        this.renderActivities();
    },

    renderActivities() {
        const container = document.getElementById('activityList');
        if (!container) return;
        container.innerHTML = this.activities.map(a =>
            `<div class="activity-item">
                <span class="activity-time">${a.time}</span>
                <span class="activity-text">${this.escapeHtml(a.text)}</span>
            </div>`
        ).join('');
    },

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },

    async pcExecute(tool, args) {
        this.addPCActivity(`Executing: ${tool}`);
        try {
            const resp = await fetch('/api/pc/execute', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tool, arguments: args }),
            });
            const data = await resp.json();
            if (data.success) {
                this.addPCActivity(`OK: ${tool}`);
                if (data.result && typeof data.result === 'object') {
                    console.log('PC Agent result:', data.result);
                }
            } else {
                this.addPCActivity(`FAIL: ${data.error || data.message || 'unknown'}`);
            }
        } catch (e) {
            this.addPCActivity(`Error: ${e.message}`);
        }
    },

    async refreshPCPanel() {
        try {
            const resp = await fetch('/api/pc/status');
            const data = await resp.json();
            const dot = document.getElementById('pcStatusDot');
            const label = document.getElementById('pcStatusLabel');
            if (data.available) {
                dot.className = 'status-dot';
                label.textContent = 'ONLINE';
                this.setText('pcPlatform', data.platform || '-');
                this.setText('pcMode', data.permissions?.mode || '-');
            } else {
                dot.className = 'status-dot offline';
                label.textContent = 'OFFLINE';
            }
        } catch {
            document.getElementById('pcStatusDot').className = 'status-dot offline';
            document.getElementById('pcStatusLabel').textContent = 'OFFLINE';
        }
        try {
            const resp = await fetch('/api/pc/tools');
            const data = await resp.json();
            const container = document.getElementById('pcToolsList');
            if (container) {
                container.innerHTML = (data.tools || []).map(t =>
                    `<div class="pc-tool-item">
                        <span class="pc-tool-status ${t.enabled ? 'enabled' : 'disabled'}">${t.enabled ? '✓' : '✗'}</span>
                        <span>${t.name}</span>
                        <span class="pc-tool-perm">${t.permission}</span>
                    </div>`
                ).join('');
            }
        } catch {}
        try {
            const resp = await fetch('/api/pc/approval/pending');
            const data = await resp.json();
            const container = document.getElementById('pcApprovalsList');
            if (container) {
                const approvals = data.approvals || [];
                if (approvals.length === 0) {
                    container.innerHTML = '<div class="activity-item"><span class="activity-text">None</span></div>';
                } else {
                    container.innerHTML = approvals.map(a =>
                        `<div class="pc-approval-item">
                            <span>${a.tool}</span>
                            <button class="pc-action-btn pc-approve" onclick="AIRA.respondApproval('${a.id}',true)">Allow</button>
                            <button class="pc-action-btn pc-deny" onclick="AIRA.respondApproval('${a.id}',false)">Deny</button>
                        </div>`
                    ).join('');
                }
            }
        } catch {}
    },

    async respondApproval(id, approve) {
        try {
            await fetch('/api/pc/approval/respond', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ approval_id: id, approve }),
            });
            this.refreshPCPanel();
        } catch {}
    },

    async setPCMode(mode) {
        try {
            await fetch('/api/pc/permission/mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode }),
            });
            this.addPCActivity(`Mode changed to: ${mode}`);
            this.refreshPCPanel();
        } catch {}
    },

    addPCActivity(text) {
        const now = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const container = document.getElementById('pcActivityLog');
        if (!container) return;
        const div = document.createElement('div');
        div.className = 'activity-item';
        div.innerHTML = `<span class="activity-time">${now}</span><span class="activity-text">${this.escapeHtml(text)}</span>`;
        container.insertBefore(div, container.firstChild);
        while (container.children.length > 20) container.removeChild(container.lastChild);
    }
};

document.addEventListener('DOMContentLoaded', () => AIRA.init());


