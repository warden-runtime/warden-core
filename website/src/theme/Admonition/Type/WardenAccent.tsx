import React, {type ReactNode} from 'react';
import clsx from 'clsx';
import type {Props} from '@theme/Admonition/Type/Tip';
import AdmonitionLayout from '@theme/Admonition/Layout';
import IconTip from '@theme/Admonition/Icon/Tip';

const infimaClassName = 'alert alert--warden-accent';

export default function AdmonitionTypeWardenAccent(props: Props): ReactNode {
  return (
    <AdmonitionLayout
      type="warden-accent"
      icon={<IconTip />}
      {...props}
      className={clsx(infimaClassName, props.className)}>
      {props.children}
    </AdmonitionLayout>
  );
}
