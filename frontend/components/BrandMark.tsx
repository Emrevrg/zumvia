/**
 * ZUMVIA marka işareti.
 * Kaynak logodan üretilmiş şeffaf PNG; yerleşik arayüzle aynı görsel.
 */
export function BrandMark({ size = 32, wordmark = false }: { size?: number; wordmark?: boolean }) {
  return (
    <img
      src={wordmark ? "/wordmark.png" : "/logo.png"}
      alt="ZUMVIA"
      width={size}
      height={wordmark ? undefined : size}
      style={{ height: wordmark ? "auto" : size, display: "block" }}
      decoding="async"
    />
  );
}

export default BrandMark;
