import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

import styles from './index.module.css';

const DOCS_INTRO = '/docs/introduction';
const ENTERPRISE_DOCS = '/docs/getting-started/open-core-vs-enterprise';

type ValueProp = {
  anchor: string;
  title: string;
  description: string;
};

const VALUE_PROPS: ValueProp[] = [
  {
    anchor: '01',
    title: 'Every step is a transaction',
    description:
      'Durable state in Postgres. Clean failure boundaries. A queryable execution history you can inspect.',
  },
  {
    anchor: '02',
    title: 'Execution stays within bounds',
    description:
      'Enforce policy rules on tool calls and pause high-risk steps for human review before they commit.',
  },
  {
    anchor: '03',
    title: 'Failures unwind cleanly',
    description:
      'When a forward step fails, the saga automatically executes compensation steps in reverse order. No silent failures, no hidden state.',
  },
];

function HomepageHeader(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={clsx('hero hero--warden', styles.heroBanner)}>
      <div className="container">
        <Heading as="h1" className="hero__title">
          {siteConfig.title}
        </Heading>
        <p className="hero__subtitle">
          The Postgres-native runtime that keeps AI agents honest.
        </p>
        <p className={styles.heroLead}>
          Built for high-risk agent workflows where inspectable execution
          history, human oversight, and safe failure are
          requirements — all inside your own database.
        </p>
        <div className={styles.ctaActions}>
          <Link
            className="button button--warden-accent button--lg"
            to={DOCS_INTRO}>
            Read the docs
          </Link>
          <Link
            className={clsx('button button--lg', styles.buttonGhost)}
            to={ENTERPRISE_DOCS}>
            Enterprise features
          </Link>
        </div>
      </div>
    </header>
  );
}

function TrustBar(): ReactNode {
  return (
    <div className={styles.trustBar}>
      Runs inside your own VPC · Postgres-native ·
      Open core · No SaaS dependency
    </div>
  );
}

function ValueProps(): ReactNode {
  return (
    <section className={styles.valueProps}>
      <div className="container">
        <div className="row">
          {VALUE_PROPS.map(({anchor, title, description}) => (
            <div key={anchor} className="col col--4">
              <span className={styles.valueAnchor} aria-hidden="true">
                {anchor}
              </span>
              <Heading as="h3" className={styles.valueTitle}>
                {title}
              </Heading>
              <p className={styles.valueDescription}>{description}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function BottomCta(): ReactNode {
  return (
    <section className={styles.bottomCta}>
      <div className="container">
        <div className={styles.ctaContainer}>
          <Heading as="h2" className={styles.ctaTitle}>
            Your infrastructure. Your database. Your rules.
          </Heading>
          <p className={styles.ctaSubtitle}>
            Run Warden open core in your own VPC, or read about compliance-grade
            enterprise features.
          </p>
          <div className={styles.ctaActions}>
            <Link
              className="button button--warden-accent button--lg"
              to={DOCS_INTRO}>
              Read the docs
            </Link>
            <Link
              className={clsx('button button--lg', styles.buttonGhost)}
              to={ENTERPRISE_DOCS}>
              Enterprise features
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout
      title={siteConfig.title}
      description="Postgres-native runtime for governed agent workflows — inspectable execution history, human oversight, and safe failure in your own database.">
      <HomepageHeader />
      <main>
        <TrustBar />
        <ValueProps />
        <BottomCta />
      </main>
    </Layout>
  );
}
