'use client';

import { useRef, useState, useEffect } from 'react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { toast } from 'sonner';

/**
 * Extracts the Flipkart Product ID (pid) from the URL query parameters.
 * @param {string} url 
 * @returns {string | null} The pid or null if not found.
 */
function extractFlipkartProductId(url) {
  try {
    const parsedUrl = new URL(url);
    // Flipkart IDs are reliably found in the 'pid' query parameter
    return parsedUrl.searchParams.get('pid');
  } catch (e) {
    return null;
  }
}

/**
 * Extracts the Amazon ASIN from the URL path.
 * @param {string} url 
 * @returns {string | null} The ASIN or null if not found.
 */
function extractAmazonAsin(url) {
  try {
    const parsedUrl = new URL(url);
    // Matches /dp/ASIN/ or /gp/product/ASIN/
    const match = parsedUrl.pathname.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i);
    return match ? match[1] : null;
  } catch (e) {
    return null;
  }
}

/**
 * Extracts the Croma Product ID (PID) from the URL path.
 * @param {string} url 
 * @returns {string | null} The numeric PID or null if not found.
 */
function extractCromaProductId(url) {
  try {
    const parsedUrl = new URL(url);
    // PID is the last segment, and must be numeric. e.g. /product/123456
    const pathParts = parsedUrl.pathname.split('/').filter(p => p.length > 0);
    const lastSegment = pathParts[pathParts.length - 1];

    // Check if the last segment looks like a numeric PID
    if (lastSegment && /^\d+$/.test(lastSegment)) {
      return lastSegment;
    }
    return null;
  } catch (e) {
    return null;
  }
}


/**
 * Extracts the Apple Part Number from an Apple product URL.
 * @param {string} url 
 * @returns {string | null} The part number or null if not found.
 */
function extractApplePartNumber(url) {
  // Regex 1: The preferred pattern for finding the SKU after '/product/' (stops at / or ?)
  let match = url.match(/\/product\/([^/?]+)/i);
  if (match && match[1]) {
    return match[1]; 
  }

  // Regex 2: Fallback to find any alphanumeric pattern with a slash, common in Apple SKUs
  match = url.match(/([A-Z0-9]{5,}[A-Z0-9]\/[A-Z0-9])/i);
  if (match && match[1]) {
    if (match[1].length > 7) { 
        return match[1];
    }
  }
  
  return null;
}


/**
 * Derives the storeType, determines if the ID field should be shown,
 * and extracts the part number if possible.
 * @param {string} url 
 * @returns {object} { storeType, showPartNumber, extractedPartNumber }
 */
function getStoreDetails(url) {
  const lowerUrl = url.toLowerCase();
  
  // --- Stores requiring client-side ID extraction (and thus showing the field) ---
  if (lowerUrl.includes('apple.com')) {
    const partNumber = extractApplePartNumber(lowerUrl);
    return { storeType: 'unicorn', showPartNumber: true, extractedPartNumber: partNumber };
  }
  
  if (lowerUrl.includes('flipkart.com')) {
    const productId = extractFlipkartProductId(lowerUrl);
    return { storeType: 'unknown', showPartNumber: true, extractedPartNumber: productId }; 
  }

  if (lowerUrl.includes('amazon.in')) {
    const productId = extractAmazonAsin(lowerUrl);
    return { storeType: 'unknown', showPartNumber: true, extractedPartNumber: productId }; 
  }

  if (lowerUrl.includes('croma.com')) {
    const productId = extractCromaProductId(lowerUrl);
    return { storeType: 'unknown', showPartNumber: true, extractedPartNumber: productId }; 
  }
  
  // --- Stores relying ONLY on the URL (Server-side extraction/scraping). Field is HIDDEN. ---
  if (lowerUrl.includes('reliancedigital.in')) {
    return { storeType: 'reliance_digital', showPartNumber: false, extractedPartNumber: null };
  }
  if (lowerUrl.includes('iqoo.com')) {
    return { storeType: 'iqoo', showPartNumber: false, extractedPartNumber: null };
  }
  if (lowerUrl.includes('vivo.com')) {
    return { storeType: 'vivo', showPartNumber: false, extractedPartNumber: null };
  }

  // Default fallback or general case
  return { storeType: 'unknown', showPartNumber: false, extractedPartNumber: null };
}


export function AddProductForm({ addProductAction }) {
  const formRef = useRef(null);
  const [url, setUrl] = useState('');
  // State to hold the product ID/part number
  const [productId, setProductId] = useState(''); 
  
  // Derived details
  const { storeType, showPartNumber, extractedPartNumber } = getStoreDetails(url);

  // Auto-population logic
  useEffect(() => {
    // 1. If an ID was successfully extracted for any supported store, set it.
    if (extractedPartNumber) {
      setProductId(extractedPartNumber);
      return; 
    }
    
    // 2. If the URL field is empty OR if the ID field should be hidden entirely, clear the state.
    if (!url || !showPartNumber) {
        setProductId('');
        return;
    }
    
    // 3. If extraction failed for a store where the field is shown (shouldn't happen with the new logic, 
    //    but preserves manual input if an invalid URL for an otherwise supported store is pasted).
    //    We explicitly do nothing here, leaving the field blank for the user to paste a better URL.

  }, [url, extractedPartNumber, showPartNumber]); 


  async function formAction(formData) {
    // Manually append the determined storeType
    formData.append('storeType', storeType);
    
    // Manually append the productId/partNumber state value.
    // This is only relevant for the stores where showPartNumber is TRUE (Apple, Flipkart, Amazon, Croma)
    if (showPartNumber && productId) {
        // Send under both names to cover validation ('partNumber') and Prisma schema ('productId')
        formData.append('partNumber', productId);
        formData.append('productId', productId);
    }
    
    // For Reliance Digital, Vivo, iQOO (where showPartNumber is FALSE), only the URL and storeType are sent.
    // actions.js uses the URL to extract the ID on the server for these stores.

    const result = await addProductAction(formData);
    
    if (result?.error) {
      toast.error(result.error);
    } else {
      toast.success("Product added to tracker!");
      formRef.current?.reset();
      setUrl(''); 
      setProductId(''); 
    }
  }

  // Determine placeholder based on recognized store
  const placeholderText = storeType === 'unknown' 
    ? "Paste Product URL (e.g., Flipkart, Amazon, Reliance Digital, Vivo, iQOO)"
    : `Paste ${storeType.replace('_', ' ').toUpperCase()} URL`;


  return (
    <form ref={formRef} action={formAction} className="flex flex-col w-full space-y-3">
      <div className="flex w-full items-center space-x-2">
        <Input
          type="text"
          name="url"
          placeholder={placeholderText}
          required
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
        <Button type="submit">Add Product</Button>
      </div>
      
      {/* Product ID Input Field. Hidden for stores with server-side extraction (RD, Vivo, iQOO). 
          For all others, it's shown and auto-populated if a valid ID is found. */}
      {showPartNumber && (
        <Input
          type="text"
          name="partNumber" 
          value={productId} 
          onChange={(e) => setProductId(e.target.value)} 
          placeholder={storeType === 'unicorn' ? "Apple Part Number (e.g., MG6P4HN/A)" : "Product ID (Extracted from URL)"}
          required 
          className="transition-all duration-300"
        />
      )}
      
      {/* Affiliate Link */}
      <Input
        type="text"
        name="affiliateLink"
        placeholder="Your Affiliate Link (Optional)"
      />
    </form>
  );
}