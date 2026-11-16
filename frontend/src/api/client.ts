import axios from 'axios';

// Create axios instance with base configuration
const api = axios.create({
    baseURL: '/api', // Proxy will handle forwarding to backend
    withCredentials: true,
    headers: {
        'Content-Type': 'application/json',
    },
});

// Response interceptor for error handling
api.interceptors.response.use(
    (response) => response,
    (error) => {
        // Auto-redirect to /login when the HttpOnly auth cookie expires mid-session
        if (error.response?.status === 401) {
            // Avoid redirect loops if already on the login page
            if (!window.location.pathname.startsWith('/login')) {
                window.location.href = '/login';
            }
            return Promise.reject(new Error('Session expired. Please log in again.'));
        }
        // Standardize error format
        const message = error.response?.data?.detail || error.message || 'An unknown error occurred';
        console.error('API Error:', message);
        return Promise.reject(new Error(message));
    }
);

export default api;
