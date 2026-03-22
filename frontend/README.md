# TradingWeb Frontend

A modern Astro.js frontend for the TradingWeb application.

## 🚀 Getting Started

### Prerequisites
- Node.js >= 22.12.0
- Docker & Docker Compose (for full stack development)

### Development

1. Install dependencies:
   ```bash
   npm install
   ```

2. Start development server:
   ```bash
   npm run dev
   ```

3. Open [http://localhost:4321](http://localhost:4321) in your browser

### Available Scripts

- `npm run dev` - Start development server with hot reload
- `npm run build` - Build for production
- `npm run preview` - Preview production build
- `npm run check` - Run Astro type checking
- `npm run check:watch` - Run Astro type checking in watch mode
- `npm run typecheck` - Run TypeScript type checking
- `npm run lint` - Run ESLint
- `npm run lint:fix` - Run ESLint with auto-fix
- `npm run format` - Format code with Prettier
- `npm run format:check` - Check code formatting

## 📁 Project Structure

```
frontend/
├── public/                 # Static assets
├── src/
│   ├── components/         # Reusable UI components
│   │   ├── common/        # Shared components
│   │   ├── dashboard/     # Dashboard-specific components
│   │   └── trading/       # Trading-related components
│   ├── constants/         # Application constants
│   ├── hooks/             # Custom React hooks
│   ├── layouts/           # Page layouts
│   ├── lib/               # Utility libraries
│   │   ├── api/          # API client functions
│   │   └── utils/        # General utilities
│   ├── pages/             # Astro pages/routes
│   ├── styles/            # Global styles and CSS
│   ├── types/             # TypeScript type definitions
│   └── utils/             # Helper functions
├── .vscode/               # VS Code configuration
├── astro.config.mjs       # Astro configuration
├── tsconfig.json          # TypeScript configuration
├── package.json           # Dependencies and scripts
└── README.md
```

## 🛠️ Development Features

### Path Aliases
Import components and utilities using clean aliases:
```typescript
import Button from '@components/common/Button';
import { apiClient } from '@lib/api/client';
import { formatCurrency } from '@utils/currency';
```

### TypeScript Support
- Strict TypeScript configuration
- Astro component type checking
- Path mapping for clean imports

### Code Quality
- ESLint for code linting
- Prettier for code formatting
- Astro-specific linting rules

### Development Tools
- Hot module replacement (HMR)
- Source maps for debugging
- Dev toolbar for development insights

## 🔧 Configuration

### Astro Config (`astro.config.mjs`)
- Server-side rendering (SSR)
- Path aliases for clean imports
- Image optimization with Sharp
- Docker-friendly file watching

### TypeScript Config (`tsconfig.json`)
- Strict type checking
- Path mapping for imports
- Astro-specific type definitions

## 🚀 Deployment

The application is configured for Docker deployment. Use the root `docker-compose.yml` to run the full stack.

For production builds:
```bash
npm run build
npm run preview
```

Astro looks for `.astro` or `.md` files in the `src/pages/` directory. Each page is exposed as a route based on its file name.

There's nothing special about `src/components/`, but that's where we like to put any Astro/React/Vue/Svelte/Preact components.

Any static assets, like images, can be placed in the `public/` directory.

## 🧞 Commands

All commands are run from the root of the project, from a terminal:

| Command                   | Action                                           |
| :------------------------ | :----------------------------------------------- |
| `npm install`             | Installs dependencies                            |
| `npm run dev`             | Starts local dev server at `localhost:4321`      |
| `npm run build`           | Build your production site to `./dist/`          |
| `npm run preview`         | Preview your build locally, before deploying     |
| `npm run astro ...`       | Run CLI commands like `astro add`, `astro check` |
| `npm run astro -- --help` | Get help using the Astro CLI                     |

## 👀 Want to learn more?

Feel free to check [our documentation](https://docs.astro.build) or jump into our [Discord server](https://astro.build/chat).
