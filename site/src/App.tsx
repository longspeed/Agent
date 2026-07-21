import { NoiseHeroFilter } from './components/primitives'
import { BackgroundGlow, GuideLines, Navbar, Hero, HowItWorks } from './components/hero'
import { ProductMockup } from './components/mockup'
import { Pricing } from './components/pricing'
import { Trust, Integrations, Security, Faq, FinalCta, Footer } from './components/sections'

export default function App() {
  return (
    <div className="relative min-h-screen overflow-x-hidden bg-bg text-cream">
      <NoiseHeroFilter />
      <BackgroundGlow />
      <GuideLines />
      <Navbar />
      <main>
        <Hero />
        <HowItWorks />
        <ProductMockup />
        <Trust />
        <Integrations />
        <Pricing />
        <Security />
        <Faq />
        <FinalCta />
      </main>
      <Footer />
    </div>
  )
}
