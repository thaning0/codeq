function formatGreeting(name: string): string {
  return `Hello, ${name.trim()}`;
}

export function renderGreeting(name: string): string {
  return formatGreeting(name);
}
