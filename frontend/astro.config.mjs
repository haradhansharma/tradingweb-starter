// tradingweb/frontend/astro.config.mjs
// @ts-check
// @ts-ignore
import { defineConfig } from 'astro/config';

// https://astro.build/config
export default defineConfig({
  // Enable dev toolbar for better debugging
  devToolbar: {
    enabled: true,
  },

  // Server configuration for development
  server: {
    host: true, // Listen on all addresses
    port: 4321,
  },

  // Vite configuration
  vite: {
    server: {
      watch: {
        usePolling: true, // Better file watching in containers
      },
      // Enable HMR with proper host for Docker
      hmr: {
        host: 'localhost',
      },
    },
    // Optimize build
    build: {
      sourcemap: true, // Enable source maps for debugging
    },
    // Resolve aliases for cleaner imports
    resolve: {
      alias: {
        '@': '/src',
        '@components': '/src/components',
        '@layouts': '/src/layouts',
        '@pages': '/src/pages',
        '@utils': '/src/utils',
        '@types': '/src/types',
        '@styles': '/src/styles',
        '@hooks': '/src/hooks',
        '@constants': '/src/constants',
        '@lib': '/src/lib',
      },
    },
  },

  // Output configuration
  output: 'server', // SSR for better performance and SEO
  adapter: undefined, // Will be added when deploying

  // Image optimization
  image: {
    service: {
      entrypoint: 'astro/assets/services/sharp',
    },
  },
});
