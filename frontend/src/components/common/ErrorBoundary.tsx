import { Component, ReactNode, ErrorInfo } from 'react';

interface ErrorBoundaryProps {
    children: ReactNode;
}

interface ErrorBoundaryState {
    hasError: boolean;
    error?: Error;
    errorInfo?: ErrorInfo;
}

class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
    constructor(props: ErrorBoundaryProps) {
        super(props);
        this.state = { hasError: false };
    }

    static getDerivedStateFromError(error: Error): ErrorBoundaryState {
        return { hasError: true, error };
    }

    componentDidCatch(error: Error, errorInfo: ErrorInfo) {
        console.error('Error caught by boundary:', error, errorInfo);
        this.setState({ errorInfo });
    }

    handleReload = () => {
        window.location.reload();
    };

    render() {
        if (this.state.hasError) {
            return (
                <div
                    style={{ background: 'var(--bg-page)' }}
                    className="min-h-screen flex items-center justify-center p-4"
                >
                    <div
                        style={{
                            background: 'var(--bg-card)',
                            border: '1px solid var(--border-primary)',
                            borderRadius: 'var(--radius-xl)',
                            boxShadow: 'var(--shadow-lg)',
                        }}
                        className="p-8 max-w-md w-full"
                    >
                        <div
                            style={{ background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.25)' }}
                            className="flex items-center justify-center w-16 h-16 rounded-full mx-auto mb-4"
                        >
                            <svg className="w-8 h-8" style={{ color: 'var(--color-danger)' }} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                            </svg>
                        </div>

                        <h1 className="text-2xl font-bold text-center mb-2" style={{ color: 'var(--text-primary)' }}>
                            Oops! Something went wrong
                        </h1>

                        <p className="text-center mb-6" style={{ color: 'var(--text-secondary)' }}>
                            We encountered an unexpected error. Don't worry, your data is safe.
                        </p>

                        {this.state.error && (
                            <div
                                style={{ background: 'var(--bg-surface)', borderRadius: 'var(--radius-md)' }}
                                className="p-4 mb-6"
                            >
                                <p className="text-sm font-mono break-words" style={{ color: 'var(--text-secondary)' }}>
                                    {this.state.error.message}
                                </p>
                            </div>
                        )}

                        <button
                            onClick={this.handleReload}
                            className="btn btn-primary w-full"
                        >
                            Reload Page
                        </button>

                        <p className="text-sm text-center mt-4" style={{ color: 'var(--text-tertiary)' }}>
                            If the problem persists, please contact support
                        </p>
                    </div>
                </div>
            );
        }

        return this.props.children;
    }
}

export default ErrorBoundary;
