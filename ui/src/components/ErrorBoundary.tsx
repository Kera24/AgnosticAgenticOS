import { Component, type ReactNode } from "react";
import { AlertTriangle } from "lucide-react";

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="empty-state" role="alert">
          <AlertTriangle size={28} aria-hidden />
          <p className="empty-title">This view failed to render</p>
          <p className="mono">{this.state.error.message}</p>
          <button
            className="btn"
            onClick={() => this.setState({ error: null })}
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
