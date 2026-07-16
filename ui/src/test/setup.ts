import "@testing-library/jest-dom/vitest";

// jsdom has no EventSource; provide an inert stub so LiveProvider mounts.
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, ((event: MessageEvent) => void)[]>();
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    const list = this.listeners.get(type) ?? [];
    list.push(listener);
    this.listeners.set(type, list);
  }
  emit(type: string, data: unknown) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data: JSON.stringify(data) } as MessageEvent);
    }
  }
  close() {}
}

(globalThis as Record<string, unknown>).EventSource = FakeEventSource;
(globalThis as Record<string, unknown>).__fakeEventSource = FakeEventSource;

// clipboard for copy buttons
Object.assign(navigator, {
  clipboard: { writeText: () => Promise.resolve() },
});
