// @ts-check
import { defineConfig } from 'astro/config';
import node from '@astrojs/node';
import tailwindcss from '@tailwindcss/vite';
import alpinejs from '@astrojs/alpinejs';
import sitemap from '@astrojs/sitemap';

// https://astro.build/config
export default defineConfig({
  site: process.env.SITE_URL || 'https://yourdomain.com',

  output: 'server',
  adapter: node({
    mode: 'standalone'
  }),

  vite: {
    plugins: [tailwindcss()],
    server: {
      proxy: {
        '/api': {
          target: process.env.BACKEND_URL || 'http://tbackend:8000',  // always proxy
          changeOrigin: true,
        },
        '/ws': {
          target: process.env.BACKEND_URL_WS || 'ws://tbackend:8000',  // always proxy
          ws: true,
        },
      },
    },
    resolve: {
      alias: { 
        '@': '/src',
        '@components': '/src/components',
        '@layouts': '/src/layouts',
        '@assets': '/src/assets',
        '@utils': '/src/utils',
        '@styles': '/src/styles', 
        '@stores': '/src/stores'  
      },
    },
  },

  integrations: [alpinejs(), sitemap()],

  server: {
    // Allows Docker to access the dev server
    host: '0.0.0.0',
    port: process.env.PORT ? parseInt(process.env.PORT) : 4321,
  },

});