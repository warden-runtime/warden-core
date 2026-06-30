import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';
import type * as OpenApiPlugin from 'docusaurus-plugin-openapi-docs';

const GITHUB_ORG = 'warden-runtime';
const GITHUB_REPO = 'warden-core';
const GITHUB_URL = `https://github.com/${GITHUB_ORG}/${GITHUB_REPO}`;
const LICENSE_URL = `${GITHUB_URL}/blob/master/LICENSE`;

const config: Config = {
  title: 'Warden',
  tagline: 'Postgres-native engine for governed autonomous workflows',
  favicon: 'img/favicon.ico',

  future: {
    v4: true,
  },

  url: 'https://warden-runtime.org',
  baseUrl: '/',

  organizationName: 'warden-runtime',
  projectName: 'warden-core',

  onBrokenLinks: 'throw',

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  markdown: {
    mermaid: true,
  },

  themes: ['@docusaurus/theme-mermaid', 'docusaurus-theme-openapi-docs'],

  plugins: [
    [
      '@docusaurus/plugin-client-redirects',
      {
        redirects: [
          {
            from: '/docs/intro',
            to: '/docs/introduction',
          },
          {
            from: '/architecture',
            to: '/docs/advanced/architecture',
          },
          {
            from: '/compensation',
            to: '/docs/guides/manifests/compensation',
          },
          {
            from: '/audit-versioning',
            to: '/docs/getting-started/open-core-vs-enterprise',
          },
          {
            from: '/docs/advanced/operations/stuck-sagas',
            to: '/docs/guides/cli/saga-recovery',
          },
          {
            from: '/docs/advanced/stuck-sagas',
            to: '/docs/guides/cli/saga-recovery',
          },
          {
            from: '/docs/advanced/operations/audit-versioning',
            to: '/docs/getting-started/open-core-vs-enterprise',
          },
          {
            from: '/docs/advanced/operations/enterprise-governance',
            to: '/docs/getting-started/open-core-vs-enterprise',
          },
          {
            from: '/docs/concepts/open-core-vs-enterprise',
            to: '/docs/getting-started/open-core-vs-enterprise',
          },
          {
            from: '/docs/getting-started/playground',
            to: '/docs/getting-started/demo-mock-llm-and-mcp',
          },
          {
            from: '/docs/getting-started/telemetry',
            to: '/docs/getting-started/demo-observe-execution-timing',
          },
          {
            from: '/docs/getting-started/gateway',
            to: '/docs/getting-started/demo-quickstart',
          },
          {
            from: '/docs/getting-started/observe-first-saga',
            to: '/docs/getting-started/demo-observe-execution-timing',
          },
          {
            from: '/docs/concepts/agentic-atomicity',
            to: '/docs/concepts/durable-execution',
          },
          {
            from: '/docs/getting-started/quickstart',
            to: '/docs/getting-started/demo-quickstart',
          },
          {
            from: '/docs/advanced/demo-github-mcp',
            to: '/docs/getting-started/demo-github-mcp',
          },
          {
            from: '/docs/guides/demo-github-mcp',
            to: '/docs/getting-started/demo-github-mcp',
          },
          {
            from: '/docs/guides/mcp-and-tools',
            to: '/docs/guides/manifests/mcp-and-tools',
          },
          {
            from: '/docs/guides/when-cel',
            to: '/docs/guides/manifests/when-cel',
          },
          {
            from: '/docs/guides/policies',
            to: '/docs/guides/manifests/policies',
          },
          {
            from: '/docs/api/warden-engine-api',
            to: '/docs/api/api-reference',
          },
        ],
      },
    ],
    [
      'docusaurus-plugin-openapi-docs',
      {
        id: 'engine-api',
        docsPluginId: 'classic',
        config: {
          engine: {
            specPath: 'openapi/engine.openapi.json',
            outputDir: '../docs/api',
            sidebarOptions: {
              groupPathsBy: 'tag',
            },
          } satisfies OpenApiPlugin.Options,
        },
      },
    ],
  ],

  presets: [
    [
      'classic',
      {
        docs: {
          path: '../docs',
          sidebarPath: './sidebars.ts',
          exclude: ['**/dev/**', '**/docs-todo.md'],
          docItemComponent: '@theme/ApiItem',
          admonitions: {
            keywords: ['warden-accent'],
            extendDefaults: true,
          },
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/warden-social-card.png',
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Warden',
      logo: {
        alt: 'Warden',
        src: 'img/warden-logo.png',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'mainSidebar',
          position: 'left',
          label: 'Docs',
        },
        {
          href: GITHUB_URL,
          position: 'right',
          className: 'header-github-link',
          'aria-label': 'GitHub repository',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {
              label: 'Introduction',
              to: '/docs/introduction',
            },
            {
              label: 'Getting started',
              to: '/docs/getting-started/prerequisites',
            },
            {
              label: 'Architecture',
              to: '/docs/advanced/architecture',
            },
            {
              label: 'API Reference',
              to: '/docs/api/api-reference',
            },
          ],
        },
        {
          title: 'Project',
          items: [
            {
              label: 'GitHub',
              href: GITHUB_URL,
            },
            {
              label: 'Apache License 2.0',
              href: LICENSE_URL,
            },
            {
              label: 'Open Core vs Enterprise',
              to: '/docs/getting-started/open-core-vs-enterprise',
            },
          ],
        },
      ],
      copyright: `Copyright © 2026 The Warden Authors. Licensed under the <a href="${LICENSE_URL}">Apache License, Version 2.0</a>.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
    },
    mermaid: {
      theme: {light: 'base', dark: 'base'},
      options: {
        htmlLabels: false,
        themeVariables: {
          fontSize: '14px',
        },
      },
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
