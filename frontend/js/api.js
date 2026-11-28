/**
 * Smart Farm IoT - Backend Integration API Client
 * Integrasi frontend ke backend Flask REST API (/api/v1 & /api/sensors)
 */

(function (window) {
    'use strict';

    const DEFAULT_BASE_URL = (
        window.location.protocol.startsWith('http') && window.location.port === '5000'
    ) ? window.location.origin : 'http://localhost:5000';

    class SmartFarmApiClient {
        constructor() {
            this.baseUrl = localStorage.getItem('smartfarm_base_url') || DEFAULT_BASE_URL;
            this.controlToken = localStorage.getItem('smartfarm_control_token') || 'replace-with-a-long-random-control-token';
            this.adminToken = localStorage.getItem('smartfarm_admin_token') || 'replace-with-a-long-random-admin-token';
            this.isConnected = false;
            this.isSyncing = false;
            this.lastSyncTime = null;
            this.pollTimer = null;
            this.listeners = {
                sync: [],
                connection: [],
                event: []
            };

            // Cached Live Data
            this.state = {
                summary: null,
                devices: {},
                telemetry: {
                    DEV_001: [],
                    DEV_002: [],
                    DEV_003: [],
                    DEV_004: []
                },
                group2Latest: null,
                events: [],
                rfidCards: []
            };
        }

        setBaseUrl(url) {
            this.baseUrl = url.replace(/\/+$/, '');
            localStorage.setItem('smartfarm_base_url', this.baseUrl);
            this.syncNow();
        }

        setControlToken(token) {
            this.controlToken = token.trim();
            localStorage.setItem('smartfarm_control_token', this.controlToken);
        }

        on(eventType, callback) {
            if (this.listeners[eventType]) {
                this.listeners[eventType].push(callback);
            }
        }

        emit(eventType, data) {
            if (this.listeners[eventType]) {
                this.listeners[eventType].forEach(cb => {
                    try { cb(data); } catch (e) { console.error('Listener error:', e); }
                });
            }
        }

        async request(endpoint, options = {}) {
            const url = `${this.baseUrl}${endpoint}`;
            const headers = Object.assign({
                'Accept': 'application/json'
            }, options.headers || {});

            if (options.body && typeof options.body === 'object' && !(options.body instanceof FormData)) {
                headers['Content-Type'] = 'application/json';
                options.body = JSON.stringify(options.body);
            }

            options.headers = headers;

            try {
                const response = await fetch(url, options);
                if (response.status === 204) {
                    return null;
                }
                const contentType = response.headers.get('content-type') || '';
                const data = contentType.includes('application/json')
                    ? await response.json()
                    : await response.text();

                if (!response.ok) {
                    const err = new Error(data && data.error ? data.error.message : `HTTP ${response.status}`);
                    err.status = response.status;
                    err.data = data;
                    throw err;
                }
                return data;
            } catch (err) {
                throw err;
            }
        }

        // ───────── API ENDPOINTS ─────────

        async checkHealth() {
            try {
                const res = await this.request('/api/v1/health');
                this._setConnected(res && res.status === 'ok');
                return res;
            } catch (err) {
                this._setConnected(false);
                return null;
            }
        }

        async getSummary() {
            return await this.request('/api/v1/dashboard/summary');
        }

        async getDevices() {
            return await this.request('/api/v1/devices');
        }

        async getDevice(deviceId) {
            return await this.request(`/api/v1/devices/${encodeURIComponent(deviceId)}`);
        }

        async getTelemetry(deviceId, perPage = 20) {
            return await this.request(`/api/v1/telemetry?device_id=${encodeURIComponent(deviceId)}&per_page=${perPage}`);
        }

        async getEvents(params = {}) {
            const query = new URLSearchParams(params).toString();
            return await this.request(`/api/v1/events${query ? '?' + query : ''}`);
        }

        async sendCommand(deviceId, actuatorId, command, parameters = {}) {
            return await this.request(`/api/v1/devices/${encodeURIComponent(deviceId)}/commands`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${this.controlToken}`
                },
                body: {
                    actuator_id: actuatorId,
                    command: command,
                    parameters: parameters
                }
            });
        }

        async getCommand(commandId) {
            return await this.request(`/api/v1/commands/${encodeURIComponent(commandId)}`);
        }

        async getRfidCards() {
            return await this.request('/api/v1/rfid-cards');
        }

        async addRfidCard(uid, label = 'Kartu Peternak') {
            return await this.request('/api/v1/rfid-cards', {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${this.controlToken}`
                },
                body: { uid, label }
            });
        }

        async deleteRfidCard(uid) {
            return await this.request(`/api/v1/rfid-cards/${encodeURIComponent(uid)}`, {
                method: 'DELETE',
                headers: {
                    'Authorization': `Bearer ${this.controlToken}`
                }
            });
        }

        // Group 2 Endpoints
        async getGroup2Latest() {
            return await this.request('/api/sensors/latest');
        }

        async getGroup2History() {
            return await this.request('/api/sensors/history');
        }

        async postGroup2Sensors(payload) {
            return await this.request('/api/sensors', {
                method: 'POST',
                body: payload
            });
        }

        // ───────── SYNC ENGINE ─────────

        _setConnected(connected) {
            if (this.isConnected !== connected) {
                this.isConnected = connected;
                this.emit('connection', { isConnected: connected, baseUrl: this.baseUrl });
            }
        }

        async syncNow() {
            if (this.isSyncing) return;
            this.isSyncing = true;

            try {
                // Test backend health first
                const health = await this.checkHealth();
                if (!health) {
                    this.isSyncing = false;
                    return false;
                }

                // Fetch core dashboard data in parallel
                const [summaryRes, devicesRes, eventsRes, g2Res, rfidRes] = await Promise.allSettled([
                    this.getSummary(),
                    this.getDevices(),
                    this.getEvents({ per_page: 15 }),
                    this.getGroup2Latest(),
                    this.getRfidCards()
                ]);

                if (summaryRes.status === 'fulfilled') {
                    this.state.summary = summaryRes.value;
                }

                if (devicesRes.status === 'fulfilled' && devicesRes.value.items) {
                    devicesRes.value.items.forEach(dev => {
                        this.state.devices[dev.device_id] = dev;
                    });
                }

                if (eventsRes.status === 'fulfilled' && eventsRes.value.items) {
                    this.state.events = eventsRes.value.items;
                }

                if (g2Res.status === 'fulfilled') {
                    this.state.group2Latest = g2Res.value;
                }

                if (rfidRes.status === 'fulfilled' && rfidRes.value.items) {
                    this.state.rfidCards = rfidRes.value.items;
                }

                // Telemetry for active devices
                const telemetryPromises = ['DEV_001', 'DEV_002', 'DEV_003', 'DEV_004'].map(devId =>
                    this.getTelemetry(devId, 10).catch(() => null)
                );
                const telemetryResults = await Promise.all(telemetryPromises);
                ['DEV_001', 'DEV_002', 'DEV_003', 'DEV_004'].forEach((devId, idx) => {
                    if (telemetryResults[idx] && telemetryResults[idx].items) {
                        this.state.telemetry[devId] = telemetryResults[idx].items;
                    }
                });

                this.lastSyncTime = new Date();
                this._setConnected(true);
                this.emit('sync', this.state);
                return true;
            } catch (err) {
                this._setConnected(false);
                return false;
            } finally {
                this.isSyncing = false;
            }
        }

        startPolling(intervalMs = 4000) {
            this.stopPolling();
            this.syncNow();
            this.pollTimer = setInterval(() => {
                this.syncNow();
            }, intervalMs);
        }

        stopPolling() {
            if (this.pollTimer) {
                clearInterval(this.pollTimer);
                this.pollTimer = null;
            }
        }
    }

    window.SmartFarmAPI = new SmartFarmApiClient();

})(window);

