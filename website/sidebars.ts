import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';
import apiSidebar from '../docs/api/sidebar';

// Manual API workflow guides: Guides → API (explicit item order below).
// Auto-generated OpenAPI MDX: top-level API Reference category (apiSidebar from gen-api-docs).
// Do not merge them — avoids alphabetized endpoint pages interleaving with overview/start-and-monitor.

const sidebars: SidebarsConfig = {
  mainSidebar: [
    'introduction',
    {
      type: 'category',
      label: 'Core concepts',
      collapsed: false,
      items: [
        'concepts/terminology',
        'concepts/durable-execution',
        'concepts/lifecycle',
      ],
    },
    {
      type: 'category',
      label: 'Getting started',
      collapsed: false,
      items: [
        {
          type: 'category',
          label: 'Setup',
          collapsed: false,
          items: [
            'getting-started/prerequisites',
            'getting-started/installation',
          ],
        },
        {
          type: 'category',
          label: 'Demos',
          collapsed: false,
          items: [
            'getting-started/demo-mock-llm-and-mcp',
            'getting-started/demo-observe-execution-timing',
            'getting-started/demo-quickstart',
            'getting-started/demo-github-mcp',
          ],
        },
        {
          type: 'category',
          label: 'Reference',
          collapsed: false,
          items: [
            'getting-started/configuration',
            'getting-started/troubleshooting',
          ],
        },
        'getting-started/open-core-vs-enterprise',
      ],
    },
    {
      type: 'category',
      label: 'Guides',
      collapsed: false,
      items: [
        {
          type: 'category',
          label: 'Manifests and artifacts',
          collapsed: false,
          items: [
            'guides/manifests/overview',
            'guides/manifests/worker-manifests',
            'guides/manifests/saga-manifests',
            'guides/manifests/prompts',
            'guides/manifests/mcp-and-tools',
            'guides/manifests/when-cel',
            'guides/manifests/policies',
            'guides/manifests/compensation',
          ],
        },
        {
          type: 'category',
          label: 'CLI',
          collapsed: false,
          items: [
            'guides/cli/overview',
            'guides/cli/deploy-and-list',
            'guides/cli/start-and-monitor',
            'guides/cli/hitl-review',
            'guides/cli/saga-recovery',
          ],
        },
        {
          type: 'category',
          label: 'API',
          collapsed: false,
          items: [
            'guides/api/overview',
            'guides/api/deploy-and-list',
            'guides/api/start-and-monitor',
            'guides/api/hitl',
            'guides/api/recovery',
          ],
        },
        'guides/observability',
      ],
    },
    {
      type: 'category',
      label: 'API Reference',
      collapsed: true,
      items: apiSidebar,
    },
    {
      type: 'category',
      label: 'Advanced',
      collapsed: false,
      items: [
        'advanced/testing',
        'advanced/architecture',
        'advanced/extending-warden',
        'advanced/migrations-and-schema',
      ],
    },
  ],
};

export default sidebars;
