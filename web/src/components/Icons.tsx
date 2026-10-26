type IconProps = { size?: number; className?: string };

const S = (size?: number) => ({ width: size ?? 16, height: size ?? 16 });

export function IconLogo({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <rect x="5" y="5" width="14" height="14" rx="2" transform="rotate(45 12 12)" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="12" cy="9.5" r="1.8" fill="currentColor" />
    </svg>
  );
}

export function IconExt({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path d="M7 17 17 7M9 7h8v8" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function IconCopy({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <rect x="9" y="9" width="11" height="11" rx="2" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M5 15V6a2 2 0 0 1 2-2h9" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function IconYes({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 18 18" className={className} aria-label="yes" role="img">
      <circle cx="9" cy="9" r="8" fill="currentColor" />
      <path d="M5.2 9.4l2.5 2.5 5-5.6" fill="none" stroke="#0c0c10" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IconOk({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 18 18" className={className} aria-hidden="true">
      <path d="M4.5 9.6l3 3 6-6.6" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IconNo({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 18 18" className={className} aria-hidden="true">
      <circle cx="9" cy="9" r="7.2" fill="none" stroke="currentColor" strokeWidth="1.2" />
      <path d="M6.5 6.5l5 5M11.5 6.5l-5 5" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

export function IconSearch({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <circle cx="11" cy="11" r="6.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M15.8 15.8 20 20" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function IconClose({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path d="M6 6l12 12M18 6 6 18" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function IconArrow({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path d="M4 12h15m-6-6 6 6-6 6" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IconLayers({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path d="m12 3 9 5-9 5-9-5 9-5Z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="m3 13 9 5 9-5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" opacity="0.55" />
    </svg>
  );
}

export function IconGrid({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <rect x="4" y="4" width="7" height="7" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <rect x="13" y="4" width="7" height="7" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <rect x="4" y="13" width="7" height="7" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <rect x="13" y="13" width="7" height="7" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

export function IconGithub({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path
        fill="currentColor"
        d="M12 2C6.48 2 2 6.58 2 12.25c0 4.53 2.87 8.37 6.84 9.73.5.1.68-.22.68-.49 0-.24-.01-.88-.01-1.73-2.78.62-3.37-1.37-3.37-1.37-.45-1.18-1.11-1.5-1.11-1.5-.91-.63.07-.62.07-.62 1 .07 1.53 1.06 1.53 1.06.9 1.57 2.36 1.12 2.94.85.09-.66.35-1.12.63-1.37-2.22-.26-4.56-1.14-4.56-5.07 0-1.12.39-2.03 1.03-2.75-.1-.26-.45-1.3.1-2.7 0 0 .84-.28 2.75 1.05a9.36 9.36 0 0 1 5 0c1.91-1.33 2.75-1.05 2.75-1.05.55 1.4.2 2.44.1 2.7.64.72 1.03 1.63 1.03 2.75 0 3.94-2.34 4.8-4.57 5.06.36.32.68.94.68 1.9 0 1.37-.01 2.47-.01 2.81 0 .27.18.6.69.49A10.05 10.05 0 0 0 22 12.25C22 6.58 17.52 2 12 2Z"
      />
    </svg>
  );
}
