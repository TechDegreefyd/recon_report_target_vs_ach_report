from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter

styles = getSampleStyleSheet()

subjects = {
    "Accountancy": [
        ("Financial statements are prepared:", "Primarily for the benefit of persons outside of the business organization."),
        ("The basic purpose of an accounting system is to:", "Meet an organization's need for accounting information as efficiently as possible."),
        ("Information is cost effective when:", "The value of the information exceeds the cost of producing it."),
        ("Financial reporting is primarily directed toward:", "Investors and creditors."),
        ("All are characteristics of managerial accounting except:", "Information must be developed in conformity with generally accepted accounting principles or with income tax regulations."),
        ("A complete set of financial statements would include all except:", "Statement of projected cash flows for 2000."),
        ("A managerial accounting report is more likely to:", "Be tailored to the specific needs of an individual decision maker."),
        ("The nature of an asset is best described as:", "An economic resource owned by a business and expected to benefit future operations."),
        ("The balance sheet item representing owner investment is:", "Owner's equity."),
        ("Arguments against cost principle are based on:", "Continued inflation."),
    ],
    "Aptitude": [
        ("SINK : FLOAT : : ATTACK : ____", "DEFEND"),
        ("INDIA : DELHI : : BANGLADESH : ____", "DHAKA"),
        ("RACE : TRACK : : BOXING : ____", "RING"),
        ("LION : DEN : : ESKIMO : ____", "IGLOO"),
        ("MOON : SATELLITE : : EARTH : ____", "PLANET"),
        ("CAR : GARAGE : : AEROPLANE : ____", "HANGER"),
        ("DOG : RABIES : : BATS : ____", "EBOLA"),
        ("MAN : BIOGRAPHY : : MOUNTAINS : ____", "OROGRAPHY"),
        ("SON : DAUGHTER : : HORSE : ____", "MARE"),
        ("CATTLE : SHED : : DOG : ____", "KENNEL"),
    ],
    "Basic Computer Knowledge": [
        ("Printer resolution is measured by the number of:", "Dots per inch (dpi)"),
        ("Who developed Linux?", "Linus Torvalds"),
        ("Seek time is:", "The time required to move the access arm to the proper cylinder"),
        ("Difference between virus and worm:", "A virus attaches itself to another file, while a worm exists independently"),
        ("Difference between Internet and Intranet:", "Internet is worldwide, Intranet is internal"),
        ("ISP stands for:", "Internet Service Provider"),
        ("Increasing order of magnitude:", "kilo, mega, giga, tera"),
        ("Acrobat Reader was developed by:", "Adobe"),
        ("CSS stands for:", "Cascading Style Sheets"),
        ("Synchronous communication is:", "Discussion via chat (instant messaging)"),
    ],
    "Biology": [
        ("The term species was coined by:", "John Ray"),
        ("New Systematics is also known as:", "Biosystematics"),
        ("Wrongly matched pair:", "Puccinia \u2013 Smut"),
        ("Nuclear membrane is absent in:", "Nostoc"),
        ("Chlamydomonas and Chlorella belong to:", "Protista"),
        ("Single-celled eukaryotes are included in:", "Protista"),
        ("Maximum nutritional diversity is found in:", "Monera"),
        ("Common feature in fungi, algae and moss protonema:", "Multiplication by fragmentation"),
        ("Wrong statement about viruses:", "They have ability to synthesize nucleic acids and proteins."),
        ("Correctly assigned organism:", "Yeast used in making bread and beer is a fungus"),
    ],
    "Chemistry": [
        ("The statement(s) regarding defects in solids is (are):", "Trapping of an electron in the lattice leads to the formation of F-centre."),
        ("The compound that obeys the octet rule is:", "CO<sub>2</sub>"),
        ("Number of lone pairs in PCl<sub>5</sub>, IF<sub>5</sub>, SOF<sub>4</sub> and XeOF<sub>4</sub> are:", "0, 1, 0 and 1"),
        ("The first law of thermodynamics is conservation of:", "Energy"),
        ("What potential should the fuel cell battery generate (under standard conditions)?", "1.23 V"),
        ("Which factors would increase the rate of a chemical reaction?", "I, II and III"),
        ("Going down in a group from F to I, which property increases?", "Ionic radius"),
        ("Which is the best reducing agent?", "Cs"),
        ("Red and white phosphorous will differ but not in:", "Exhibiting phosphorescence"),
        ("Which bond has the least energy?", "Te-Te"),
    ],
    "Chemistry (Leet)": [
        ("Which of the following is not an element?", "Silica"),
        ("Lanthanides in periodic table belongs to:", "f-block"),
        ("The element of second period is called:", "Representative element"),
        ("The most electronegative element in the periodic table:", "Fluorine"),
        ("The units of molarity is:", "Mole/liter"),
        ("Which method of expressing concentration is independent of temperature?", "Molality"),
        ("In a solid lubricant, the ____ will be low:", "Coefficient of friction"),
        ("The electronic configuration of an atom can be defined by:", "All of these"),
        ("1s<super>2</super>, 2s<super>2</super>, 2p<super>5</super> represents which element?", "Fluorine"),
        ("The first noble gas in the periodic table:", "Helium"),
    ],
    "Economics": [
        ("The curve showing possibilities of production is known as:", "Production possibility curve"),
        ("Which definition of Economics is associated with Lionel Robbins?", "Scarcity definition"),
        ("Which embodies a widely accepted definition of economics?", "A study of mankind in the ordinary business of life"),
        ("The fundamental problem faced by an economy is one of:", "Scarcity of resources and multiplicity of wants"),
        ("State whether Economics is:", "A science or an art depending on who uses Economics and for what purpose"),
        ("Micro economic theory studies how a free enterprise economy determines:", "The price of goods"),
        ("Which of the following is incorrect about microeconomics?", "Microeconomics is concerned primarily with comparative statics rather than dynamics"),
        ("The meaning of the word 'economic' is most closely associated with:", "Scarce"),
        ("Microeconomics studies the decision making behaviour of:", "An individual or household"),
        ("The subject matter of economics is the study of:", "Scarcity and Choice"),
    ],
    "English": [
        ("Database means:", "Collection of information"),
        ("Genial means:", "Cheerful"),
        ("I have never ________ all through the night before:", "had to work"),
        ("The stolen paintings were eventually restored ______ their rightful owner:", "to"),
        ("According to the passage, computers can be:", "Customised"),
        ("With the introduction of computers, human potential has been:", "Accelerated"),
        ("Antonym of tall:", "Short"),
        ("Antonym of hot:", "Cold"),
        ("Antonym of good:", "Bad"),
        ("Antonym of foolish:", "Wise"),
    ],
    "General Knowledge / General Studies": [
        ("Correct chronological order of formation of Haryana, Sikkim, Arunachal Pradesh and Nagaland as states:", "Nagaland \u2013 Haryana \u2013 Sikkim \u2013 Arunachal Pradesh"),
        ("Currency of Japan:", "Yen"),
        ("First Indian woman in Space:", "Kalpana Chawla"),
        ("Capital of Brazil:", "Bras\u00edlia"),
        ("Chief Minister of Haryana State:", "Nayab Singh Saini"),
        ("First port developed after independence in Gujarat:", "Kandla"),
        ("Which state touches maximum number of states in India?", "Uttar Pradesh"),
        ("Number of commands of Indian Air Force:", "Seven"),
        ("Television was invented by:", "J L Baird"),
        ("Air conditioner was discovered by:", "Carrier"),
    ],
    "Geography": [
        ("Which process is a degradational process?", "Erosion"),
        ("Tensile forces create?", "Faults"),
        ("The point where earthquake generates is called?", "Focus"),
        ("P-Waves travel in:", "Both solid and liquid"),
        ("Most shaking felt from an earthquake is due to?", "Rayleigh wave"),
        ("The Discovery of India is written by?", "J.L. Nehru"),
        ("Economic geography is the most developed branch of?", "Human geography"),
        ("Who defined economic geography as scientific analysis of world territories' influence on goods?", "Zimmarman"),
        ("Growth is a quantitative term which has:", "Materialistic connotation"),
        ("Who said economic geography encompasses all forms of materials, resources, activities?", "Zimmarman"),
    ],
    "History": [
        ("The arrival of Vasco da Gama in Calicut, India:", "1498"),
        ("Diu was the colony of the:", "Portuguese"),
        ("In 1612, which country established a trading post in Gujarat?", "British"),
        ("In 1614 Sir Thomas Roe was instructed by ________ to visit Jahangir:", "James I"),
        ("In 1661 the company obtained ________ from Charles II:", "Bombay"),
        ("In 1650 Gabriel Boughton obtained a license for trade in:", "Bengal"),
        ("Year of the Battle of Plassey:", "1757"),
        ("Year of the Battle of Wandiwash:", "1760"),
        ("Year of the Battle of Buxar:", "1764"),
        ("Warren Hastings was appointed as the Governor of ________ in 1772:", "Bengal"),
    ],
    "Legal Studies": [
        ("Regarding women workforce participation, correct statement(s):", "2 only"),
        ("Jamnagar-based cluster of Ayurveda institutes is located in:", "Gujarat"),
        ("Headquarters of International Commission of Jurists:", "Geneva"),
        ("Which state decided no Gorkha citizen will be prosecuted under CAA 1955?", "Assam"),
        ("Which body made observations on Rs.223 crore IAF expenditure?", "Comptroller and Auditor General"),
        ("Which institution resolved to clear pending cases against legislators?", "Supreme Court of India"),
        ("First country to approve needle-free, inhaled Covid-19 vaccine:", "China"),
        ("Which state launched 'Xpress Clinic' diagnostic service?", "Kerala"),
        ("Who formed Aarzi Hukumat (government-in-exile) in Junagarh?", "Samaldas Gandhi"),
        ("Work field of Jibanananda Das:", "Literature"),
    ],
    "LLB": [
        ("The first major environmental conference was:", "Stockholm, 1972"),
        ("The principle 'Common but Differentiated Responsibilities' originated from:", "Rio Earth Summit, 1992"),
        ("The Paris Agreement 2015 focuses on:", "Limiting global temperature rise below 2\u00b0C"),
        ("India ratified the Paris Agreement in:", "2016"),
        ("The UNEP headquarters is in:", "Nairobi"),
        ("Kyoto Protocol 1997 deals with:", "Greenhouse gas emission reduction"),
        ("Stockholm Conference Declaration contains:", "26 Principles"),
        ("Rio Declaration introduced:", "Agenda 21 and Sustainable Development Goals"),
        ("The Paris Agreement replaces:", "Kyoto Protocol after 2020"),
        ("India's National Action Plan on Climate Change (NAPCC) was launched in:", "2008"),
    ],
}


def build_pdf(path, title, data):
    doc = SimpleDocTemplate(
        path,
        pagesize=letter,
        rightMargin=30,
        leftMargin=30,
        topMargin=30,
        bottomMargin=20,
    )
    elems = []
    elems.append(Paragraph(title, styles["Title"]))
    elems.append(Spacer(1, 20))

    for subject, qas in data.items():
        elems.append(Paragraph(subject, styles["Heading1"]))
        elems.append(Spacer(1, 10))

        elems.append(Paragraph("<b>Questions</b>", styles["Heading2"]))
        for i, (q, a) in enumerate(qas, start=1):
            elems.append(Paragraph(f"{i}. {q}", styles["BodyText"]))
            elems.append(Spacer(1, 5))

        elems.append(Spacer(1, 12))

        elems.append(Paragraph("<b>Answers</b>", styles["Heading2"]))

        table_data = [["Q.No", "Answer"]]
        for i, (q, a) in enumerate(qas, start=1):
            table_data.append([str(i), a])

        table = Table(table_data, colWidths=[60, 420])
        table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("GRID", (0, 0), (-1, -1), 1, colors.black),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ])
        )

        elems.append(table)
        elems.append(PageBreak())

    doc.build(elems)


combined = "Full_Questions_Answers.pdf"
build_pdf(combined, "Full Questions and Answers", subjects)

print(f"PDF generated: {combined}")
