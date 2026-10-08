import './globals.css';

export const metadata = {
  title: 'NetScope — Network Discovery Lab',
  description: 'Discover and configure real network devices and EVE-NG labs.',
};

export default function RootLayout({ children }) {
  return <html lang="th"><body>{children}</body></html>;
}
