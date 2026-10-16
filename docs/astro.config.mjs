// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://coffernest-docs.vkfolio.com',
  integrations: [
    starlight({
      title: 'Coffernest',
      description: 'Guides for the Coffernest iPhone app and the open-source Coffernest server.',
      logo: { src: './src/assets/icon.png', alt: 'Coffernest' },
      favicon: '/favicon.png',
      customCss: ['./src/styles/apple.css'],
      social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/vkfolio/coffernest' }],
      editLink: { baseUrl: 'https://github.com/vkfolio/coffernest/edit/main/docs/' },
      lastUpdated: true,
      sidebar: [
        { label: 'Start here', items: [{ autogenerate: { directory: 'start' } }] },
        { label: 'Using the app', items: [{ autogenerate: { directory: 'app' } }] },
        { label: 'Family', items: [{ autogenerate: { directory: 'family' } }] },
        { label: 'Assistant and AI', items: [{ autogenerate: { directory: 'ai' } }] },
        { label: 'Run your own server', items: [{ autogenerate: { directory: 'server' } }] },
        { label: 'API', items: [{ autogenerate: { directory: 'api' } }] },
      ],
    }),
  ],
});
