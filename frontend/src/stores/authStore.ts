/**
 * Auth Store for MarketPulse (Astro + Alpine.js)
 * ==============================================
 * Manages JWT authentication state for the Astro SPA.
 *
 * Architecture:
 *   - Uses Django Ninja JWT for token-based auth
 *   - OTP verification required for new registrations
 *   - Stores tokens in localStorage (access + refresh)
 *   - Auto-refreshes access token before expiry
 *   - Provides reactive state for Alpine.js UI bindings
 *
 * Auth flow:
 *   POST /api/auth/register   → create inactive user + send OTP
 *   POST /api/auth/verify-otp  → verify OTP, activate account
 *   POST /api/auth/resend-otp  → resend OTP (rate limited)
 *   POST /api/auth/pair        → obtain JWT tokens (requires active user)
 *   POST /api/auth/refresh     → refresh access token
 *   GET  /api/auth/me          → user profile (Bearer: <access>)
 *   POST /api/auth/change-password → change password (JWT required)
 *   POST /api/auth/forgot-password → request password reset OTP
 *   POST /api/auth/reset-password  → reset password with OTP
 */

// ── Types ──

export interface UserProfile {
  id: number;
  username: string;
  email: string | null;
  is_verified: boolean;
  bio: string | null;
  risk_tolerance: string | null;
  tracked_assets: string[];
  notify_strong_signals: boolean;
  notify_whale_activity: boolean;
  notify_market_shifts: boolean;
}

export interface AuthState {
  isAuthenticated: boolean;
  isLoading: boolean;
  user: UserProfile | null;
  accessToken: string | null;
  refreshToken: string | null;
  error: string | null;
}

export interface BrokerCredential {
  id: number;
  broker: string;
  label: string;
  is_testnet: boolean;
  is_active: boolean;
  last_validated: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// ── Constants ──

const ACCESS_TOKEN_KEY = 'mp_access_token';
const REFRESH_TOKEN_KEY = 'mp_refresh_token';
const REFRESH_THRESHOLD_MS = 5 * 60 * 1000; // Refresh 5 min before expiry
let refreshTimer: ReturnType<typeof setTimeout> | null = null;

// ── JWT helpers ──

function decodeJwtPayload(token: string): Record<string, any> | null {
  try {
    const base64Url = token.split('.')[1];
    const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
    const jsonPayload = decodeURIComponent(
      atob(base64)
        .split('')
        .map((c) => '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2))
        .join('')
    );
    return JSON.parse(jsonPayload);
  } catch {
    return null;
  }
}

function getTokenExpiry(token: string): number | null {
  const payload = decodeJwtPayload(token);
  if (!payload || !payload.exp) return null;
  return payload.exp * 1000; // Convert seconds to ms
}

function isTokenExpired(token: string): boolean {
  const expiry = getTokenExpiry(token);
  if (!expiry) return true;
  return Date.now() >= expiry;
}

function isTokenExpiringSoon(token: string): boolean {
  const expiry = getTokenExpiry(token);
  if (!expiry) return true;
  return Date.now() >= expiry - REFRESH_THRESHOLD_MS;
}

// ── Auth Store Factory ──

export function createAuthStore() {
  const state: AuthState = {
    isAuthenticated: false,
    isLoading: false,
    user: null,
    accessToken: localStorage.getItem(ACCESS_TOKEN_KEY),
    refreshToken: localStorage.getItem(REFRESH_TOKEN_KEY),
    error: null,
  };

  // If we have tokens, mark as authenticated until we verify
  if (state.accessToken && !isTokenExpired(state.accessToken)) {
    state.isAuthenticated = true;
  }

  return {
    // ── State (reactive for Alpine.js) ──
    get isAuthenticated() { return state.isAuthenticated; },
    set isAuthenticated(v: boolean) { state.isAuthenticated = v; },
    get isLoading() { return state.isLoading; },
    set isLoading(v: boolean) { state.isLoading = v; },
    get user() { return state.user; },
    set user(v: UserProfile | null) { state.user = v; },
    get accessToken() { return state.accessToken; },
    get refreshToken() { return state.refreshToken; },
    get error() { return state.error; },
    set error(v: string | null) { state.error = v; },

    get username(): string {
      return state.user?.username || '';
    },

    get displayName(): string {
      return state.user?.username || 'Guest';
    },

    // ── Registration (creates inactive user, sends OTP) ──

    /**
     * Register a new account. User is created as inactive.
     * OTP is sent to the user's email.
     * Does NOT auto-login — user must verify OTP first.
     * Returns registration email on success for the OTP page.
     */
    async register(username: string, email: string, password: string): Promise<{ success: boolean; email?: string; error?: string }> {
      state.isLoading = true;
      state.error = null;

      try {
        const resp = await fetch('/api/auth/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, email, password }),
        });

        if (!resp.ok) {
          const data = await resp.json().catch(() => ({}));
          state.error = data.message || 'Registration failed';
          state.isLoading = false;
          return { success: false, error: data.message || 'Registration failed.' };
        }

        const data = await resp.json();
        state.isLoading = false;
        return { success: true, email: data.email };
      } catch (e) {
        state.error = 'Network error. Please try again.';
        state.isLoading = false;
        return { success: false, error: 'Network error. Please try again.' };
      }
    },

    /**
     * Verify OTP code to activate user account.
     */
    async verifyOtp(username: string, code: string): Promise<{ success: boolean; message: string }> {
      state.isLoading = true;
      state.error = null;

      try {
        const resp = await fetch('/api/auth/verify-otp', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, code }),
        });

        const data = await resp.json();

        if (!resp.ok || !data.success) {
          state.error = data.message || 'Verification failed';
          state.isLoading = false;
          return { success: false, message: data.message || 'Verification failed.' };
        }

        state.isLoading = false;
        return { success: true, message: data.message };
      } catch (e) {
        state.error = 'Network error. Please try again.';
        state.isLoading = false;
        return { success: false, message: 'Network error. Please try again.' };
      }
    },

    /**
     * Resend OTP verification code.
     */
    async resendOtp(username: string): Promise<{ success: boolean; message: string }> {
      state.isLoading = true;
      state.error = null;

      try {
        const resp = await fetch('/api/auth/resend-otp', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username }),
        });

        const data = await resp.json();

        if (!resp.ok) {
          state.error = data.message || 'Failed to resend code';
          state.isLoading = false;
          return { success: false, message: data.message || 'Failed to resend code.' };
        }

        state.isLoading = false;
        return { success: true, message: data.message };
      } catch (e) {
        state.error = 'Network error. Please try again.';
        state.isLoading = false;
        return { success: false, message: 'Network error. Please try again.' };
      }
    },

    // ── Actions ──

    /**
     * Login with username + password.
     * Stores JWT tokens and fetches user profile.
     * Returns detailed result including verification status.
     */
    async login(username: string, password: string): Promise<{ success: boolean; requiresVerification?: boolean; error?: string }> {
      state.isLoading = true;
      state.error = null;

      try {
        const resp = await fetch('/api/auth/pair', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password }),
        });

        const data = await resp.json().catch(() => ({}));

        if (!resp.ok) {
          // Check if account needs verification
          if (resp.status === 403 && data.requires_verification) {
            state.isLoading = false;
            return { success: false, requiresVerification: true, error: data.message };
          }

          state.error = data.message || data.detail || 'Login failed';
          state.isLoading = false;
          return { success: false, error: data.message || data.detail || 'Login failed.' };
        }

        // Store tokens
        state.accessToken = data.access;
        state.refreshToken = data.refresh;
        localStorage.setItem(ACCESS_TOKEN_KEY, data.access);
        localStorage.setItem(REFRESH_TOKEN_KEY, data.refresh);
        state.isAuthenticated = true;

        // Schedule auto-refresh
        this._scheduleRefresh(data.access);

        // Fetch user profile
        await this.fetchProfile();

        state.isLoading = false;
        return { success: true };
      } catch (e) {
        state.error = 'Network error. Please try again.';
        state.isLoading = false;
        return { success: false, error: 'Network error. Please try again.' };
      }
    },

    /**
     * Logout — clear tokens and state.
     */
    logout() {
      state.isAuthenticated = false;
      state.user = null;
      state.accessToken = null;
      state.refreshToken = null;
      state.error = null;
      localStorage.removeItem(ACCESS_TOKEN_KEY);
      localStorage.removeItem(REFRESH_TOKEN_KEY);

      if (refreshTimer) {
        clearTimeout(refreshTimer);
        refreshTimer = null;
      }
    },

    /**
     * Fetch current user profile from /api/auth/me.
     */
    async fetchProfile(): Promise<UserProfile | null> {
      if (!state.accessToken) return null;

      try {
        const resp = await fetch('/api/auth/me', {
          headers: {
            Authorization: `Bearer ${state.accessToken}`,
          },
        });

        if (!resp.ok) {
          // Token might be expired — try refresh
          const refreshed = await this.refreshAccessToken();
          if (!refreshed) {
            this.logout();
            return null;
          }

          // Retry with new token
          const retryResp = await fetch('/api/auth/me', {
            headers: {
              Authorization: `Bearer ${state.accessToken}`,
            },
          });
          if (!retryResp.ok) {
            this.logout();
            return null;
          }

          state.user = await retryResp.json();
          return state.user;
        }

        state.user = await resp.json();
        return state.user;
      } catch (e) {
        console.error('[Auth] Failed to fetch profile:', e);
        return null;
      }
    },

    /**
     * Refresh the access token using the refresh token.
     */
    async refreshAccessToken(): Promise<boolean> {
      if (!state.refreshToken) return false;

      try {
        const resp = await fetch('/api/auth/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh: state.refreshToken }),
        });

        if (!resp.ok) {
          // Refresh token is also expired — full logout
          this.logout();
          return false;
        }

        const data = await resp.json();
        state.accessToken = data.access;
        localStorage.setItem(ACCESS_TOKEN_KEY, data.access);

        // Update refresh token if rotated
        if (data.refresh) {
          state.refreshToken = data.refresh;
          localStorage.setItem(REFRESH_TOKEN_KEY, data.refresh);
        }

        // Schedule next refresh
        this._scheduleRefresh(data.access);

        return true;
      } catch (e) {
        console.error('[Auth] Token refresh failed:', e);
        return false;
      }
    },

    /**
     * Get the Authorization header value for authenticated requests.
     */
    getAuthHeaders(): Record<string, string> {
      if (!state.accessToken) return {};
      return { Authorization: `Bearer ${state.accessToken}` };
    },

    /**
     * Change password (requires current password).
     */
    async changePassword(currentPassword: string, newPassword: string): Promise<{ success: boolean; error?: string }> {
      if (!state.accessToken) return { success: false, error: 'Not authenticated' };

      try {
        const resp = await fetch('/api/auth/change-password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...this.getAuthHeaders() },
          body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
        });

        const data = await resp.json();
        if (!resp.ok) {
          return { success: false, error: data.message || 'Failed to change password.' };
        }
        return { success: true };
      } catch (e) {
        return { success: false, error: 'Network error.' };
      }
    },

    /**
     * Request password reset OTP.
     */
    async forgotPassword(username: string): Promise<{ success: boolean; message: string }> {
      try {
        const resp = await fetch('/api/auth/forgot-password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username }),
        });

        const data = await resp.json();
        return { success: resp.ok, message: data.message };
      } catch (e) {
        return { success: false, message: 'Network error.' };
      }
    },

    /**
     * Reset password with OTP code.
     */
    async resetPassword(username: string, code: string, newPassword: string): Promise<{ success: boolean; message: string }> {
      try {
        const resp = await fetch('/api/auth/reset-password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, code, new_password: newPassword }),
        });

        const data = await resp.json();
        return { success: resp.ok, message: data.message };
      } catch (e) {
        return { success: false, message: 'Network error.' };
      }
    },

    // ── Broker Credentials API ──

    async fetchCredentials(): Promise<BrokerCredential[]> {
      if (!state.accessToken) return [];
      try {
        const resp = await fetch('/api/auth/credentials', {
          headers: this.getAuthHeaders(),
        });
        if (!resp.ok) return [];
        return await resp.json();
      } catch {
        return [];
      }
    },

    async saveCredential(data: {
      broker: string;
      label: string;
      api_key: string;
      api_secret: string;
      is_testnet?: boolean;
    }): Promise<{ success: boolean; error?: string }> {
      if (!state.accessToken) return { success: false, error: 'Not authenticated' };
      try {
        const resp = await fetch('/api/auth/credentials', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...this.getAuthHeaders() },
          body: JSON.stringify(data),
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          return { success: false, error: err.message || 'Failed to save credentials' };
        }
        return { success: true };
      } catch (e) {
        return { success: false, error: 'Network error' };
      }
    },

    async deleteCredential(id: number): Promise<boolean> {
      if (!state.accessToken) return false;
      try {
        const resp = await fetch(`/api/auth/credentials/${id}`, {
          method: 'DELETE',
          headers: this.getAuthHeaders(),
        });
        return resp.ok;
      } catch {
        return false;
      }
    },

    // ── Internal ──

    /**
     * Schedule an automatic token refresh before it expires.
     */
    _scheduleRefresh(accessToken: string) {
      if (refreshTimer) {
        clearTimeout(refreshTimer);
        refreshTimer = null;
      }

      const expiry = getTokenExpiry(accessToken);
      if (!expiry) return;

      const delay = Math.max(
        expiry - Date.now() - REFRESH_THRESHOLD_MS,
        60_000 // Minimum 1 minute delay
      );

      console.log(`[Auth] Scheduling token refresh in ${Math.round(delay / 1000)}s`);
      refreshTimer = setTimeout(() => {
        this.refreshAccessToken();
      }, delay);
    },

    /**
     * Initialize auth state on page load.
     * Validates existing tokens and fetches profile.
     */
    async init(): Promise<void> {
      if (!state.accessToken) {
        state.isAuthenticated = false;
        return;
      }

      // Check if access token is expired
      if (isTokenExpired(state.accessToken)) {
        // Try refresh
        const refreshed = await this.refreshAccessToken();
        if (!refreshed) {
          this.logout();
          return;
        }
      } else if (isTokenExpiringSoon(state.accessToken)) {
        // Refresh proactively
        await this.refreshAccessToken();
      } else {
        // Schedule refresh for later
        this._scheduleRefresh(state.accessToken);
      }

      // Fetch profile
      await this.fetchProfile();

      if (state.user) {
        state.isAuthenticated = true;
      }
    },

    /**
     * Clear any error message.
     */
    clearError() {
      state.error = null;
    },
  };
}

// ── Singleton ──

export type AuthStore = ReturnType<typeof createAuthStore>;
