# -*- coding: utf-8 -*-
"""Text for the three pages a visitor can read without an account.

These are rendered by the server rather than by the single-page app, because a
crawler that arrives at /terms must be handed the words, not a loading spinner
and a promise to fetch them. Same reason they live here rather than in
i18n.js: the shell has not loaded yet when these are served.

Short on purpose. A page of boilerplate nobody reads is not a policy, it is a
place to hide one; every line here says something a reader can check against
what the software does.
"""

L = ("en", "bg", "de", "ro", "el", "tr", "it", "ru", "es", "fr", "pl", "uk", "pt")

PAGES = {
 "terms": {
  "title": {
   "en": "Terms", "bg": "Условия", "de": "Bedingungen", "ro": "Condiții",
   "el": "Όροι", "tr": "Koşullar", "it": "Condizioni", "ru": "Условия",
   "es": "Condiciones", "fr": "Conditions", "pl": "Warunki", "uk": "Умови",
   "pt": "Condições"},
  "lead": {
   "en": "What this installation is, and what it is not.",
   "bg": "Какво представлява тази инсталация и какво не е.",
   "de": "Was diese Installation ist und was nicht.",
   "ro": "Ce este această instalare și ce nu este.",
   "el": "Τι είναι αυτή η εγκατάσταση και τι δεν είναι.",
   "tr": "Bu kurulum nedir ve ne değildir.",
   "it": "Che cosa è questa installazione e che cosa non è.",
   "ru": "Что представляет собой эта установка и чем она не является.",
   "es": "Qué es esta instalación y qué no es.",
   "fr": "Ce qu'est cette installation, et ce qu'elle n'est pas.",
   "pl": "Czym jest ta instalacja, a czym nie jest.",
   "uk": "Що таке ця інсталяція і чим вона не є.",
   "pt": "O que é esta instalação e o que não é."},
  "items": [
   ({"en": "Who runs this instance",
     "bg": "Кой поддържа тази инсталация",
     "de": "Wer diese Instanz betreibt",
     "ro": "Cine administrează această instanță",
     "el": "Ποιος λειτουργεί αυτή την εγκατάσταση",
     "tr": "Bu kurulumu kim işletiyor",
     "it": "Chi gestisce questa istanza",
     "ru": "Кто содержит эту установку",
     "es": "Quién opera esta instalación",
     "fr": "Qui exploite cette instance",
     "pl": "Kto prowadzi tę instalację",
     "uk": "Хто утримує цю інсталяцію",
     "pt": "Quem opera esta instância"},
    {"en": "This installation is run by the company that deployed it. Seam is the software it runs, not a service standing between you and your counterparty.",
     "bg": "Тази инсталация се поддържа от фирмата, която я е пуснала. Seam е софтуерът, който тя ползва, а не услуга, застанала между вас и насрещната страна.",
     "de": "Diese Installation betreibt das Unternehmen, das sie aufgesetzt hat. Seam ist die Software, die dort läuft, kein Dienst zwischen Ihnen und Ihrer Gegenseite.",
     "ro": "Această instalare este administrată de firma care a pus-o în funcțiune. Seam este programul pe care îl rulează, nu un serviciu așezat între dvs. și cealaltă parte.",
     "el": "Την εγκατάσταση τη λειτουργεί η εταιρεία που την έστησε. Το Seam είναι το λογισμικό που τρέχει, όχι υπηρεσία ανάμεσα σε εσάς και την άλλη πλευρά.",
     "tr": "Bu kurulumu, onu devreye alan şirket işletir. Seam, o şirketin çalıştırdığı yazılımdır; sizinle karşı taraf arasında duran bir hizmet değildir.",
     "it": "Questa installazione è gestita dall'azienda che l'ha messa in opera. Seam è il software che vi gira, non un servizio che si frappone tra lei e la controparte.",
     "ru": "Эту установку содержит компания, которая её развернула. Seam это программа, которая на ней работает, а не служба между вами и встречной стороной.",
     "es": "Esta instalación la opera la empresa que la puso en marcha. Seam es el software que ejecuta, no un servicio interpuesto entre usted y su contraparte.",
     "fr": "Cette installation est exploitée par l'entreprise qui l'a mise en place. Seam est le logiciel qu'elle exécute, non un service placé entre vous et votre contrepartie.",
     "pl": "Tę instalację prowadzi firma, która ją uruchomiła. Seam jest oprogramowaniem, które na niej działa, a nie usługą stojącą między Państwem a drugą stroną.",
     "uk": "Цю інсталяцію утримує компанія, яка її розгорнула. Seam це програма, що на ній працює, а не служба між вами та протилежною стороною.",
     "pt": "Esta instalação é operada pela empresa que a colocou em funcionamento. O Seam é o software que ela executa, não um serviço interposto entre si e a sua contraparte."}),

   ({"en": "What is agreed here binds the two companies",
     "bg": "Договореното тук обвързва двете фирми",
     "de": "Was hier vereinbart wird, bindet die beiden Firmen",
     "ro": "Ce se convine aici obligă cele două firme",
     "el": "Ό,τι συμφωνείται εδώ δεσμεύει τις δύο εταιρείες",
     "tr": "Burada anlaşılan, iki şirketi bağlar",
     "it": "Quanto concordato qui vincola le due aziende",
     "ru": "Согласованное здесь обязывает две компании",
     "es": "Lo acordado aquí obliga a las dos empresas",
     "fr": "Ce qui est convenu ici engage les deux entreprises",
     "pl": "To, co uzgodnione tutaj, wiąże obie firmy",
     "uk": "Погоджене тут зобов'язує дві компанії",
     "pt": "O que aqui é acordado vincula as duas empresas"},
    {"en": "Prices, deadlines and scope accepted in a record are agreements between the two companies. The software records them and proves who accepted what and when; it is not a party to them.",
     "bg": "Цени, срокове и обхват, приети по дадена поръчка, са договорености между двете фирми. Софтуерът ги записва и доказва кой какво и кога е приел; той не е страна по тях.",
     "de": "Preise, Termine und Umfang, die in einem Vorgang angenommen werden, sind Vereinbarungen zwischen den beiden Firmen. Die Software hält fest, wer wann was angenommen hat; Partei ist sie nicht.",
     "ro": "Prețurile, termenele și obiectul acceptate într-o comandă sunt înțelegeri între cele două firme. Programul le consemnează și dovedește cine, ce și când a acceptat; el nu este parte.",
     "el": "Τιμές, προθεσμίες και αντικείμενο που γίνονται δεκτά σε μια παραγγελία είναι συμφωνίες μεταξύ των δύο εταιρειών. Το λογισμικό τις καταγράφει και αποδεικνύει ποιος δέχτηκε τι και πότε· δεν είναι συμβαλλόμενο μέρος.",
     "tr": "Bir kayıtta kabul edilen fiyat, süre ve kapsam, iki şirket arasındaki anlaşmalardır. Yazılım bunları kaydeder ve kimin neyi ne zaman kabul ettiğini kanıtlar; taraf değildir.",
     "it": "Prezzi, termini e oggetto accettati in una pratica sono accordi tra le due aziende. Il software li registra e prova chi ha accettato che cosa e quando; non ne è parte.",
     "ru": "Цены, сроки и объём, принятые по заказу, это договорённости между двумя компаниями. Программа их фиксирует и доказывает, кто что и когда принял; стороной она не является.",
     "es": "Los precios, plazos y alcance aceptados en un expediente son acuerdos entre las dos empresas. El software los registra y prueba quién aceptó qué y cuándo; no es parte de ellos.",
     "fr": "Prix, délais et périmètre acceptés dans un dossier sont des accords entre les deux entreprises. Le logiciel les consigne et prouve qui a accepté quoi et quand; il n'y est pas partie.",
     "pl": "Ceny, terminy i zakres przyjęte w zamówieniu są uzgodnieniami między dwiema firmami. Oprogramowanie je odnotowuje i dowodzi, kto co i kiedy przyjął; stroną nie jest.",
     "uk": "Ціни, строки та обсяг, прийняті в замовленні, є домовленостями між двома компаніями. Програма їх фіксує й доводить, хто що і коли прийняв; стороною вона не є.",
     "pt": "Preços, prazos e âmbito aceites num registo são acordos entre as duas empresas. O software regista-os e prova quem aceitou o quê e quando; não é parte neles."}),

   ({"en": "Where the data sits",
     "bg": "Къде стоят данните",
     "de": "Wo die Daten liegen",
     "ro": "Unde stau datele",
     "el": "Πού βρίσκονται τα δεδομένα",
     "tr": "Veriler nerede duruyor",
     "it": "Dove stanno i dati",
     "ru": "Где лежат данные",
     "es": "Dónde están los datos",
     "fr": "Où se trouvent les données",
     "pl": "Gdzie leżą dane",
     "uk": "Де лежать дані",
     "pt": "Onde ficam os dados"},
    {"en": "Records, documents and files are held by this installation. Nothing is sent elsewhere unless the operator switches on mail, push, off-site backup or a payment processor, and the Data page reports which of those are on right now.",
     "bg": "Поръчките, документите и файловете стоят в тази инсталация. Нищо не заминава другаде, освен ако операторът не включи поща, push, външен архив или платежен доставчик, а страницата Данни отчита кои от тях са включени в момента.",
     "de": "Vorgänge, Dokumente und Dateien liegen in dieser Installation. Nichts geht anderswohin, solange der Betreiber nicht Mail, Push, externe Sicherung oder einen Zahlungsdienst einschaltet, und die Datenseite berichtet, was davon gerade an ist.",
     "ro": "Comenzile, documentele și fișierele stau în această instalare. Nimic nu pleacă în altă parte decât dacă operatorul pornește poșta, push, copia externă sau un procesator de plăți, iar pagina Date raportează care dintre ele sunt pornite acum.",
     "el": "Οι εγγραφές, τα έγγραφα και τα αρχεία μένουν σε αυτή την εγκατάσταση. Τίποτα δεν φεύγει αλλού εκτός αν ο διαχειριστής ενεργοποιήσει αλληλογραφία, push, εξωτερικό αντίγραφο ή πάροχο πληρωμών, και η σελίδα Δεδομένα αναφέρει τι είναι ενεργό τώρα.",
     "tr": "Kayıtlar, belgeler ve dosyalar bu kurulumda durur. İşletici posta, push, dış yedek veya bir ödeme sağlayıcısı açmadıkça hiçbir şey başka yere gitmez; Veri sayfası şu anda hangilerinin açık olduğunu bildirir.",
     "it": "Pratiche, documenti e file restano in questa installazione. Nulla va altrove se il gestore non attiva posta, push, backup esterno o un processore di pagamenti, e la pagina Dati riporta quali di questi sono attivi ora.",
     "ru": "Записи, документы и файлы лежат в этой установке. Никуда больше ничего не уходит, пока оператор не включит почту, push, внешнюю копию или платёжного провайдера, а страница Данные показывает, что включено сейчас.",
     "es": "Los expedientes, documentos y archivos están en esta instalación. Nada sale a otro sitio salvo que el operador active correo, push, copia externa o una pasarela de pago, y la página Datos informa de cuáles están activos ahora.",
     "fr": "Dossiers, documents et fichiers restent dans cette installation. Rien ne part ailleurs tant que l'exploitant n'active pas la messagerie, le push, la sauvegarde externe ou un prestataire de paiement, et la page Données indique lesquels sont actifs.",
     "pl": "Zamówienia, dokumenty i pliki leżą w tej instalacji. Nic nie trafia gdzie indziej, dopóki operator nie włączy poczty, push, kopii zewnętrznej lub dostawcy płatności, a strona Dane pokazuje, co jest teraz włączone.",
     "uk": "Записи, документи та файли лежать у цій інсталяції. Нікуди більше нічого не йде, доки оператор не ввімкне пошту, push, зовнішню копію чи платіжного провайдера, а сторінка Дані показує, що ввімкнено зараз.",
     "pt": "Registos, documentos e ficheiros ficam nesta instalação. Nada sai para outro lado a não ser que o operador ligue correio, push, cópia externa ou um processador de pagamentos, e a página Dados indica quais estão ligados."}),

   ({"en": "Availability and liability",
     "bg": "Достъпност и отговорност",
     "de": "Verfügbarkeit und Haftung",
     "ro": "Disponibilitate și răspundere",
     "el": "Διαθεσιμότητα και ευθύνη",
     "tr": "Erişilebilirlik ve sorumluluk",
     "it": "Disponibilità e responsabilità",
     "ru": "Доступность и ответственность",
     "es": "Disponibilidad y responsabilidad",
     "fr": "Disponibilité et responsabilité",
     "pl": "Dostępność i odpowiedzialność",
     "uk": "Доступність і відповідальність",
     "pt": "Disponibilidade e responsabilidade"},
    {"en": "The software is provided as it stands, with no warranty. Uptime, backups and support are the responsibility of whoever runs this installation, and are whatever they have agreed with you.",
     "bg": "Софтуерът се предоставя както е, без гаранция. Наличността, архивите и поддръжката са отговорност на този, който поддържа инсталацията, и са такива, каквито е уговорил с вас.",
     "de": "Die Software wird bereitgestellt, wie sie ist, ohne Gewähr. Verfügbarkeit, Sicherungen und Unterstützung verantwortet, wer diese Installation betreibt, und zwar so, wie mit Ihnen vereinbart.",
     "ro": "Programul este pus la dispoziție așa cum este, fără garanție. Disponibilitatea, copiile și asistența cad în sarcina celui care administrează instalarea, în măsura convenită cu dvs.",
     "el": "Το λογισμικό παρέχεται ως έχει, χωρίς εγγύηση. Η διαθεσιμότητα, τα αντίγραφα και η υποστήριξη βαρύνουν όποιον λειτουργεί την εγκατάσταση, όπως έχει συμφωνηθεί μαζί σας.",
     "tr": "Yazılım olduğu gibi, garantisiz sunulur. Erişilebilirlik, yedekler ve destek kurulumu işletenin sorumluluğundadır ve sizinle kararlaştırdığı ölçüdedir.",
     "it": "Il software è fornito così com'è, senza garanzia. Disponibilità, backup e assistenza competono a chi gestisce l'installazione, nei termini concordati con lei.",
     "ru": "Программа предоставляется как есть, без гарантий. Доступность, резервные копии и поддержка на том, кто содержит установку, и в тех рамках, о которых он с вами договорился.",
     "es": "El software se ofrece tal cual, sin garantía. La disponibilidad, las copias y el soporte corresponden a quien opera la instalación, en los términos acordados con usted.",
     "fr": "Le logiciel est fourni en l'état, sans garantie. Disponibilité, sauvegardes et assistance relèvent de qui exploite l'installation, dans les termes convenus avec vous.",
     "pl": "Oprogramowanie udostępniane jest w takim stanie, w jakim jest, bez gwarancji. Dostępność, kopie i wsparcie należą do tego, kto prowadzi instalację, na warunkach uzgodnionych z Państwem.",
     "uk": "Програма надається як є, без гарантій. Доступність, резервні копії та підтримка лежать на тому, хто утримує інсталяцію, у межах, погоджених із вами.",
     "pt": "O software é fornecido tal como está, sem garantia. Disponibilidade, cópias e apoio são da responsabilidade de quem opera a instalação, nos termos acordados consigo."}),
  ],
 },
}

from public_more import PRIVACY, FAQ                          # noqa: E402

PAGES["privacy"] = PRIVACY
PAGES["faq"] = FAQ


def page(name, lang):
    """(title, lead, [(heading, body), ...]) in the reader's language."""
    p = PAGES[name]
    lang = lang if lang in L else "en"
    pick = lambda d: d.get(lang) or d["en"]
    return pick(p["title"]), pick(p["lead"]), [(pick(h), pick(b)) for h, b in p["items"]]


#: The link back to the application, in the reader's language.
HOME = {"en": "Seam", "bg": "Seam", "de": "Seam", "ro": "Seam", "el": "Seam",
        "tr": "Seam", "it": "Seam", "ru": "Seam", "es": "Seam", "fr": "Seam",
        "pl": "Seam", "uk": "Seam", "pt": "Seam"}
