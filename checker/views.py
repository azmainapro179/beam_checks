from django.http import JsonResponse
from django.shortcuts import render
from django.core.files.storage import FileSystemStorage

from .forms import DXFUploadForm
from .parser import ColumnScheduleParser, DecisionEngine

def get_structure_types(request):

    location = request.GET.get("location")
    occupancy = request.GET.get("occupancy")
    spt = float(request.GET.get("spt", 0))

    engine = DecisionEngine()

    types = engine.get_result(
        location,
        occupancy,
        spt
    )
    
    if types == "B":
        structure_types = ["SMRF","IMRF","OMRF"]
    elif types == "C":
        structure_types = ["SMRF","IMRF"]
    elif types == "D":
        structure_types = ["SMRF"]

    return JsonResponse({
        "structure_types": structure_types
    })


def upload_dxf(request):
    
    locations = [
        "Bagerhat", "Bandarban", "Barguna", "Barisal", "Bhola", "Bogra",
        "Brahmanbaria", "Chandpur", "Chapainababganj", "Chittagong",
        "Chuadanga", "Comilla", "Cox's Bazar", "Dhaka", "Dinajpur",
        "Faridpur", "Feni", "Gaibandha", "Gazipur", "Gopalganj",
        "Habiganj", "Jaipurhat", "Jamalpur", "Jessore", "Jhalokati",
        "Jhenaidah", "Khagrachari", "Khulna", "Kishoreganj", "Kurigram",
        "Kushtia", "Lakshmipur", "Lalmanirhat", "Madaripur", "Magura",
        "Manikganj", "Maulvibazar", "Meherpur", "Mongla", "Munshiganj",
        "Mymensingh", "Narail", "Narayanganj", "Narsingdi", "Natore",
        "Naogaon", "Netrakona", "Nilphamari", "Noakhali", "Pabna",
        "Panchagarh", "Patuakhali", "Pirojpur", "Rajbari", "Rajshahi",
        "Rangamati", "Rangpur", "Satkhira", "Shariatpur", "Sherpur",
        "Sirajganj", "Srimangal", "Sunamganj", "Sylhet", "Tangail",
        "Thakurgaon"
    ]
    
    occupancies = [
        "Agricultural facilities", 
        "Temporary facilities",
        "Minor storage facilities",
        "Buildings and other structures where less than 300 people congregate in one area", 

        "Buildings and other structures where more than 300 people congregate in one area",
        "Buildings and other structures with day care facilities with a capacity greater than 150",
        "Buildings and other structures with elementary school or secondary school facilities with a capacity greater than 250",
        "Buildings and other structures with a capacity greater than 500 for colleges or adult education facilities",
        "Healthcare facilities with a capacity of 50 or more resident patients, but not having surgery or emergency treatment facilities",
        "Jails and detention facilities",

        "Power generating stations",
        "Water treatment facilities",
        "Sewage treatment facilities",
        "Telecommunication centers",

        "Hospitals and other healthcare facilities having surgery or emergency treatment facilities",
        "Fire, rescue, ambulance, and police stations and emergency vehicle garages",
        "Designated earthquake, hurricane, or other emergency shelters",
        "Designated emergency preparedness, communication, operation centers and other facilities required for emergency response",
        "Power generating stations and other public utility facilities required in an emergency",
        "Ancillary structures including, but not limited to, communication towers, fuel storage tanks, cooling towers",
        "Electrical substation structures, fire water storage tanks or other structures housing or supporting water, or other fire-suppression material or equipment required for operation of Occupancy Category IV structures during an emergency",
        "Aviation control towers, air traffic control centers, and emergency aircraft hangars",
        "Community water storage facilities and pump structures required to maintain water pressure for fire suppression",
        "Buildings and other structures having critical national defense functions",
    ]



    errors = []

    if request.method == "POST":

        form = DXFUploadForm(request.POST, request.FILES)

        if form.is_valid():
            file = request.FILES["file"]

            fs = FileSystemStorage()
            filename = fs.save(file.name, file)
            file_path = fs.path(filename)

            parser = ColumnScheduleParser(file_path)
            grouped = parser.run()

    else:
        form = DXFUploadForm()

    return render(request, "upload.html", {
        "form": form,
        "locations": locations,
        "occupancies": occupancies,
        "grouped": dict(sorted(grouped.items())) if 'grouped' in locals() else None,
    })