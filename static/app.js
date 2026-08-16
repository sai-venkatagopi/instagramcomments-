document.addEventListener("DOMContentLoaded", () => {
    // Determine default base URL for webhook simulation
    const currentOrigin = window.location.origin;
    const webhookInput = document.getElementById("webhook-url-input");
    if (webhookInput) {
        webhookInput.value = `${currentOrigin}/webhook`;
    }

    let activeRunId = null;

    // Elements
    const statSentEl = document.getElementById("stat-sent");
    const statQueuedEl = document.getElementById("stat-queued");
    const statBlockedEl = document.getElementById("stat-blocked");
    const statFailedEl = document.getElementById("stat-failed");

    const rulesListEl = document.getElementById("rules-list");
    const ruleCountBadge = document.getElementById("rule-count-badge");
    const createRuleForm = document.getElementById("create-rule-form");
    const keywordInput = document.getElementById("keyword-input");
    const messageInput = document.getElementById("message-input");

    const simulationForm = document.getElementById("simulation-form");
    const simStatusBox = document.getElementById("sim-status-box");
    const simStatusText = document.getElementById("sim-status-text");
    const simRunIdText = document.getElementById("sim-run-id-text");
    const checkTruthBtn = document.getElementById("check-truth-btn");
    const simSpinner = document.getElementById("sim-spinner");

    const logsTbody = document.getElementById("logs-tbody");
    const refreshLogsBtn = document.getElementById("refresh-logs-btn");

    const truthModal = document.getElementById("truth-modal");
    const closeModalBtn = document.getElementById("close-modal-btn");
    const modalBodyContent = document.getElementById("modal-body-content");

    // --- Fetch Stats ---
    async function fetchStats() {
        try {
            const res = await fetch("/stats");
            if (!res.ok) return;
            const data = await res.json();
            
            statSentEl.textContent = data.sent ?? 0;
            statQueuedEl.textContent = data.queued ?? 0;
            statBlockedEl.textContent = data.duplicates_blocked ?? 0;
            statFailedEl.textContent = data.failed ?? 0;
        } catch (err) {
            console.error("Error fetching stats:", err);
        }
    }

    // --- Fetch Rules ---
    async function fetchRules() {
        try {
            const res = await fetch("/rules");
            if (!res.ok) return;
            const rules = await res.json();

            ruleCountBadge.textContent = `${rules.length} Active`;

            if (rules.length === 0) {
                rulesListEl.innerHTML = `<div class="empty-state">No rules configured yet. Create one above!</div>`;
                return;
            }

            rulesListEl.innerHTML = rules.map(rule => `
                <div class="rule-item">
                    <div class="rule-info">
                        <span class="rule-kw">KEYWORD: "${escapeHtml(rule.keyword)}"</span>
                        <span class="rule-msg" title="${escapeHtml(rule.dm_message)}">${escapeHtml(rule.dm_message)}</span>
                    </div>
                    <button class="delete-rule-btn" data-id="${rule.rule_id}" title="Delete Rule">
                        &times;
                    </button>
                </div>
            `).join("");

            // Add delete click listeners
            document.querySelectorAll(".delete-rule-btn").forEach(btn => {
                btn.addEventListener("click", async (e) => {
                    const ruleId = e.currentTarget.getAttribute("data-id");
                    await deleteRule(ruleId);
                });
            });
        } catch (err) {
            console.error("Error fetching rules:", err);
        }
    }

    // --- Create Rule ---
    createRuleForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const keyword = keywordInput.value.trim();
        const dm_message = messageInput.value.trim();
        if (!keyword || !dm_message) return;

        try {
            const res = await fetch("/rules", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ keyword, dm_message })
            });

            if (res.ok) {
                keywordInput.value = "";
                messageInput.value = "";
                await fetchRules();
            } else {
                const err = await res.json();
                alert(`Error creating rule: ${err.detail || 'Failed'}`);
            }
        } catch (err) {
            alert(`Error creating rule: ${err.message}`);
        }
    });

    // --- Delete Rule ---
    async function deleteRule(ruleId) {
        try {
            const res = await fetch(`/rules/${ruleId}`, { method: "DELETE" });
            if (res.ok) {
                await fetchRules();
            }
        } catch (err) {
            console.error("Error deleting rule:", err);
        }
    }

    // --- Fetch Logs ---
    async function fetchLogs() {
        try {
            const res = await fetch("/api/logs?limit=50");
            if (!res.ok) return;
            const logs = await res.json();

            if (logs.length === 0) {
                logsTbody.innerHTML = `<tr><td colspan="6" class="empty-state">No DMs dispatched yet. Start a simulation or trigger a comment webhook!</td></tr>`;
                return;
            }

            logsTbody.innerHTML = logs.map(log => `
                <tr>
                    <td>
                        <span class="status-badge status-${log.status}">
                            ${log.status.replace("_", " ")}
                        </span>
                    </td>
                    <td><span class="user-tag">@${escapeHtml(log.username || log.user_id)}</span></td>
                    <td><code style="font-size: 0.8rem; color: #94a3b8;">${escapeHtml(log.comment_id)}</code></td>
                    <td><div style="max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(log.message)}</div></td>
                    <td>${log.attempts}</td>
                    <td style="color: var(--text-muted); font-size: 0.8rem;">${formatTime(log.updated_at)}</td>
                </tr>
            `).join("");

        } catch (err) {
            console.error("Error fetching logs:", err);
        }
    }

    // --- Trigger Simulation ---
    simulationForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const webhook_url = webhookInput.value.trim();
        const count = parseInt(document.getElementById("sim-count").value, 10);
        const duration_seconds = parseInt(document.getElementById("sim-duration").value, 10);

        simStatusBox.classList.remove("hidden");
        simSpinner.classList.remove("hidden");
        simStatusText.textContent = `Triggering simulation (${count} events / ${duration_seconds}s)...`;
        simRunIdText.textContent = "";
        checkTruthBtn.style.display = "none";

        try {
            const res = await fetch("/api/simulate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ webhook_url, count, duration_seconds })
            });

            if (res.ok) {
                const data = await res.json();
                activeRunId = data.run_id || data.id;
                simStatusText.textContent = `Simulation Started! Processing background load...`;
                simRunIdText.textContent = `Run ID: ${activeRunId || 'Active'}`;
                checkTruthBtn.style.display = "inline-flex";
            } else {
                const errData = await res.json();
                simSpinner.classList.add("hidden");
                simStatusText.textContent = `Simulation Failed: ${errData.detail || 'API error'}`;
            }
        } catch (err) {
            simSpinner.classList.add("hidden");
            simStatusText.textContent = `Error: ${err.message}`;
        }
    });

    // --- Fetch Simulation Ground Truth ---
    checkTruthBtn.addEventListener("click", async () => {
        if (!activeRunId) return;
        truthModal.classList.remove("hidden");
        modalBodyContent.innerHTML = `<div class="loading-text">Fetching ground truth for Run ${activeRunId}...</div>`;

        try {
            const res = await fetch(`/api/simulate/${activeRunId}/truth`);
            if (res.ok) {
                const data = await res.json();
                renderTruthData(data);
            } else {
                modalBodyContent.innerHTML = `<div class="error-text">Failed to load truth data: ${res.statusText}</div>`;
            }
        } catch (err) {
            modalBodyContent.innerHTML = `<div class="error-text">Error loading ground truth: ${err.message}</div>`;
        }
    });

    function renderTruthData(data) {
        modalBodyContent.innerHTML = `
            <div style="display: flex; flex-direction: column; gap: 1rem;">
                <p>Ground truth data returned by the mock server for validation comparison:</p>
                <div style="background: rgba(0,0,0,0.5); padding: 1rem; border-radius: 10px; font-family: var(--font-mono); font-size: 0.82rem; max-height: 400px; overflow-y: auto;">
                    <pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>
                </div>
            </div>
        `;
    }

    closeModalBtn.addEventListener("click", () => {
        truthModal.classList.add("hidden");
    });

    refreshLogsBtn.addEventListener("click", () => {
        fetchLogs();
        fetchStats();
    });

    // Helper functions
    function escapeHtml(str) {
        if (!str) return "";
        return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
    }

    function formatTime(isoStr) {
        if (!isoStr) return "";
        try {
            const date = new Date(isoStr);
            return date.toLocaleTimeString();
        } catch (e) {
            return isoStr;
        }
    }

    // Initial Load & Polling Intervals
    fetchStats();
    fetchRules();
    fetchLogs();

    setInterval(fetchStats, 1500);
    setInterval(fetchLogs, 3000);
});
